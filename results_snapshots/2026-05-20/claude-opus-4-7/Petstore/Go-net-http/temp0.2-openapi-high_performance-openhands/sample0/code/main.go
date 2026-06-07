package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/lib/pq"
)

var db *sql.DB

// ---------- Models ----------

type Pet struct {
	ID        *int64   `json:"id,omitempty"`
	Name      string   `json:"name"`
	PhotoUrls []string `json:"photoUrls"`
	Status    *string  `json:"status,omitempty"`
}

type Order struct {
	ID       *int64     `json:"id,omitempty"`
	PetID    *int64     `json:"petId,omitempty"`
	Quantity *int       `json:"quantity,omitempty"`
	ShipDate *time.Time `json:"shipDate,omitempty"`
	Status   *string    `json:"status,omitempty"`
	Complete *bool      `json:"complete,omitempty"`
}

type User struct {
	ID         *int64  `json:"id,omitempty"`
	Username   string  `json:"username"`
	FirstName  *string `json:"firstName,omitempty"`
	LastName   *string `json:"lastName,omitempty"`
	Email      *string `json:"email,omitempty"`
	Password   *string `json:"password,omitempty"`
	Phone      *string `json:"phone,omitempty"`
	UserStatus *int    `json:"userStatus,omitempty"`
}

// ---------- DB Init ----------

func initDB() error {
	host := getenv("DB_HOST", "localhost")
	port := getenv("DB_PORT", "5432")
	user := getenv("DB_USER", "postgres")
	password := getenv("DB_PASSWORD", "postgres")
	dbname := getenv("DB_NAME", "testdb")

	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, dbname)

	var err error
	db, err = sql.Open("postgres", connStr)
	if err != nil {
		return err
	}

	db.SetMaxOpenConns(50)
	db.SetMaxIdleConns(25)
	db.SetConnMaxLifetime(5 * time.Minute)

	for i := 0; i < 30; i++ {
		if err = db.Ping(); err == nil {
			break
		}
		time.Sleep(time.Second)
	}
	if err != nil {
		return err
	}

	schema := `
	CREATE TABLE IF NOT EXISTS pets (
		id BIGSERIAL PRIMARY KEY,
		name TEXT NOT NULL,
		photo_urls TEXT[] NOT NULL DEFAULT '{}',
		status TEXT
	);
	CREATE INDEX IF NOT EXISTS idx_pets_status ON pets(status);

	CREATE TABLE IF NOT EXISTS orders (
		id BIGSERIAL PRIMARY KEY,
		pet_id BIGINT,
		quantity INTEGER,
		ship_date TIMESTAMPTZ,
		status TEXT,
		complete BOOLEAN
	);

	CREATE TABLE IF NOT EXISTS users (
		id BIGSERIAL PRIMARY KEY,
		username TEXT UNIQUE NOT NULL,
		first_name TEXT,
		last_name TEXT,
		email TEXT,
		password TEXT,
		phone TEXT,
		user_status INTEGER
	);
	CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
	`
	_, err = db.Exec(schema)
	return err
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// ---------- Helpers ----------

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if v != nil {
		_ = json.NewEncoder(w).Encode(v)
	}
}

func httpError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"message": msg})
}

// ---------- Pet Handlers ----------

func handlePetRoot(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodPost:
		addPet(w, r)
	case http.MethodPut:
		updatePet(w, r)
	default:
		w.Header().Set("Allow", "POST, PUT")
		httpError(w, http.StatusMethodNotAllowed, "method not allowed")
	}
}

func addPet(w http.ResponseWriter, r *http.Request) {
	var p Pet
	if err := json.NewDecoder(r.Body).Decode(&p); err != nil {
		httpError(w, http.StatusBadRequest, "invalid input")
		return
	}
	if p.Name == "" || p.PhotoUrls == nil {
		httpError(w, http.StatusBadRequest, "invalid input")
		return
	}

	var id int64
	var err error
	if p.ID != nil {
		err = db.QueryRow(
			`INSERT INTO pets (id, name, photo_urls, status) VALUES ($1, $2, $3, $4) RETURNING id`,
			*p.ID, p.Name, pq.Array(p.PhotoUrls), p.Status,
		).Scan(&id)
	} else {
		err = db.QueryRow(
			`INSERT INTO pets (name, photo_urls, status) VALUES ($1, $2, $3) RETURNING id`,
			p.Name, pq.Array(p.PhotoUrls), p.Status,
		).Scan(&id)
	}
	if err != nil {
		httpError(w, http.StatusBadRequest, "invalid input")
		return
	}
	p.ID = &id
	writeJSON(w, http.StatusOK, p)
}

func updatePet(w http.ResponseWriter, r *http.Request) {
	var p Pet
	if err := json.NewDecoder(r.Body).Decode(&p); err != nil {
		httpError(w, http.StatusBadRequest, "invalid input")
		return
	}
	if p.ID == nil {
		httpError(w, http.StatusNotFound, "pet not found")
		return
	}
	res, err := db.Exec(
		`UPDATE pets SET name=$1, photo_urls=$2, status=$3 WHERE id=$4`,
		p.Name, pq.Array(p.PhotoUrls), p.Status, *p.ID,
	)
	if err != nil {
		httpError(w, http.StatusBadRequest, "invalid input")
		return
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		httpError(w, http.StatusNotFound, "pet not found")
		return
	}
	writeJSON(w, http.StatusOK, p)
}

func findPetsByStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		httpError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	status := r.URL.Query().Get("status")
	if status == "" {
		writeJSON(w, http.StatusOK, []Pet{})
		return
	}
	rows, err := db.Query(`SELECT id, name, photo_urls, status FROM pets WHERE status=$1`, status)
	if err != nil {
		httpError(w, http.StatusInternalServerError, "db error")
		return
	}
	defer rows.Close()
	out := make([]Pet, 0, 16)
	for rows.Next() {
		var p Pet
		var id int64
		var urls []string
		var st sql.NullString
		if err := rows.Scan(&id, &p.Name, pq.Array(&urls), &st); err != nil {
			continue
		}
		p.ID = &id
		p.PhotoUrls = urls
		if st.Valid {
			s := st.String
			p.Status = &s
		}
		out = append(out, p)
	}
	writeJSON(w, http.StatusOK, out)
}

func handlePetByID(w http.ResponseWriter, r *http.Request) {
	idStr := strings.TrimPrefix(r.URL.Path, "/pet/")
	if idStr == "" || strings.Contains(idStr, "/") {
		http.NotFound(w, r)
		return
	}
	id, err := strconv.ParseInt(idStr, 10, 64)
	if err != nil {
		httpError(w, http.StatusNotFound, "pet not found")
		return
	}
	switch r.Method {
	case http.MethodGet:
		getPetByID(w, id)
	case http.MethodDelete:
		deletePet(w, id)
	default:
		w.Header().Set("Allow", "GET, DELETE")
		httpError(w, http.StatusMethodNotAllowed, "method not allowed")
	}
}

func getPetByID(w http.ResponseWriter, id int64) {
	var p Pet
	var pid int64
	var urls []string
	var st sql.NullString
	err := db.QueryRow(`SELECT id, name, photo_urls, status FROM pets WHERE id=$1`, id).
		Scan(&pid, &p.Name, pq.Array(&urls), &st)
	if err == sql.ErrNoRows {
		httpError(w, http.StatusNotFound, "pet not found")
		return
	}
	if err != nil {
		httpError(w, http.StatusInternalServerError, "db error")
		return
	}
	p.ID = &pid
	p.PhotoUrls = urls
	if st.Valid {
		s := st.String
		p.Status = &s
	}
	writeJSON(w, http.StatusOK, p)
}

func deletePet(w http.ResponseWriter, id int64) {
	res, err := db.Exec(`DELETE FROM pets WHERE id=$1`, id)
	if err != nil {
		httpError(w, http.StatusInternalServerError, "db error")
		return
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		httpError(w, http.StatusNotFound, "pet not found")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"message": "deleted"})
}

// ---------- Order Handlers ----------

func handleOrderRoot(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		httpError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var o Order
	if err := json.NewDecoder(r.Body).Decode(&o); err != nil {
		httpError(w, http.StatusBadRequest, "invalid input")
		return
	}
	var id int64
	var err error
	if o.ID != nil {
		err = db.QueryRow(
			`INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete)
			 VALUES ($1, $2, $3, $4, $5, $6) RETURNING id`,
			*o.ID, o.PetID, o.Quantity, o.ShipDate, o.Status, o.Complete,
		).Scan(&id)
	} else {
		err = db.QueryRow(
			`INSERT INTO orders (pet_id, quantity, ship_date, status, complete)
			 VALUES ($1, $2, $3, $4, $5) RETURNING id`,
			o.PetID, o.Quantity, o.ShipDate, o.Status, o.Complete,
		).Scan(&id)
	}
	if err != nil {
		httpError(w, http.StatusBadRequest, "invalid input")
		return
	}
	o.ID = &id
	writeJSON(w, http.StatusOK, o)
}

func handleOrderByID(w http.ResponseWriter, r *http.Request) {
	idStr := strings.TrimPrefix(r.URL.Path, "/store/order/")
	if idStr == "" || strings.Contains(idStr, "/") {
		http.NotFound(w, r)
		return
	}
	id, err := strconv.ParseInt(idStr, 10, 64)
	if err != nil {
		httpError(w, http.StatusNotFound, "order not found")
		return
	}
	switch r.Method {
	case http.MethodGet:
		var o Order
		var oid int64
		var petID sql.NullInt64
		var qty sql.NullInt64
		var shipDate sql.NullTime
		var st sql.NullString
		var complete sql.NullBool
		err := db.QueryRow(
			`SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id=$1`, id,
		).Scan(&oid, &petID, &qty, &shipDate, &st, &complete)
		if err == sql.ErrNoRows {
			httpError(w, http.StatusNotFound, "order not found")
			return
		}
		if err != nil {
			httpError(w, http.StatusInternalServerError, "db error")
			return
		}
		o.ID = &oid
		if petID.Valid {
			v := petID.Int64
			o.PetID = &v
		}
		if qty.Valid {
			v := int(qty.Int64)
			o.Quantity = &v
		}
		if shipDate.Valid {
			t := shipDate.Time
			o.ShipDate = &t
		}
		if st.Valid {
			s := st.String
			o.Status = &s
		}
		if complete.Valid {
			b := complete.Bool
			o.Complete = &b
		}
		writeJSON(w, http.StatusOK, o)
	case http.MethodDelete:
		res, err := db.Exec(`DELETE FROM orders WHERE id=$1`, id)
		if err != nil {
			httpError(w, http.StatusInternalServerError, "db error")
			return
		}
		n, _ := res.RowsAffected()
		if n == 0 {
			httpError(w, http.StatusNotFound, "order not found")
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"message": "deleted"})
	default:
		w.Header().Set("Allow", "GET, DELETE")
		httpError(w, http.StatusMethodNotAllowed, "method not allowed")
	}
}

// ---------- User Handlers ----------

func handleUserRoot(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		httpError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	var u User
	if err := json.NewDecoder(r.Body).Decode(&u); err != nil {
		httpError(w, http.StatusBadRequest, "invalid input")
		return
	}
	var id int64
	err := db.QueryRow(
		`INSERT INTO users (username, first_name, last_name, email, password, phone, user_status)
		 VALUES ($1, $2, $3, $4, $5, $6, $7)
		 ON CONFLICT (username) DO UPDATE SET
		   first_name=EXCLUDED.first_name,
		   last_name=EXCLUDED.last_name,
		   email=EXCLUDED.email,
		   password=EXCLUDED.password,
		   phone=EXCLUDED.phone,
		   user_status=EXCLUDED.user_status
		 RETURNING id`,
		u.Username, u.FirstName, u.LastName, u.Email, u.Password, u.Phone, u.UserStatus,
	).Scan(&id)
	if err != nil {
		httpError(w, http.StatusBadRequest, "invalid input")
		return
	}
	u.ID = &id
	writeJSON(w, http.StatusOK, u)
}

func handleUserLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		httpError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	username := r.URL.Query().Get("username")
	password := r.URL.Query().Get("password")
	if username == "" || password == "" {
		httpError(w, http.StatusBadRequest, "Invalid credentials")
		return
	}
	var storedPassword sql.NullString
	err := db.QueryRow(`SELECT password FROM users WHERE username=$1`, username).Scan(&storedPassword)
	if err == sql.ErrNoRows || (err == nil && (!storedPassword.Valid || storedPassword.String != password)) {
		httpError(w, http.StatusBadRequest, "Invalid credentials")
		return
	}
	if err != nil {
		httpError(w, http.StatusInternalServerError, "db error")
		return
	}
	token := fmt.Sprintf("logged in user session: %d", time.Now().Unix())
	writeJSON(w, http.StatusOK, token)
}

func handleUserByName(w http.ResponseWriter, r *http.Request) {
	username := strings.TrimPrefix(r.URL.Path, "/user/")
	if username == "" || strings.Contains(username, "/") {
		http.NotFound(w, r)
		return
	}
	switch r.Method {
	case http.MethodGet:
		var u User
		var id int64
		var first, last, email, password, phone sql.NullString
		var status sql.NullInt64
		err := db.QueryRow(
			`SELECT id, username, first_name, last_name, email, password, phone, user_status
			 FROM users WHERE username=$1`, username,
		).Scan(&id, &u.Username, &first, &last, &email, &password, &phone, &status)
		if err == sql.ErrNoRows {
			httpError(w, http.StatusNotFound, "user not found")
			return
		}
		if err != nil {
			httpError(w, http.StatusInternalServerError, "db error")
			return
		}
		u.ID = &id
		if first.Valid {
			s := first.String
			u.FirstName = &s
		}
		if last.Valid {
			s := last.String
			u.LastName = &s
		}
		if email.Valid {
			s := email.String
			u.Email = &s
		}
		if password.Valid {
			s := password.String
			u.Password = &s
		}
		if phone.Valid {
			s := phone.String
			u.Phone = &s
		}
		if status.Valid {
			v := int(status.Int64)
			u.UserStatus = &v
		}
		writeJSON(w, http.StatusOK, u)
	case http.MethodPut:
		var u User
		if err := json.NewDecoder(r.Body).Decode(&u); err != nil {
			httpError(w, http.StatusBadRequest, "invalid input")
			return
		}
		newUsername := u.Username
		if newUsername == "" {
			newUsername = username
		}
		res, err := db.Exec(
			`UPDATE users SET username=$1, first_name=$2, last_name=$3, email=$4, password=$5, phone=$6, user_status=$7
			 WHERE username=$8`,
			newUsername, u.FirstName, u.LastName, u.Email, u.Password, u.Phone, u.UserStatus, username,
		)
		if err != nil {
			httpError(w, http.StatusBadRequest, "invalid input")
			return
		}
		n, _ := res.RowsAffected()
		if n == 0 {
			httpError(w, http.StatusNotFound, "user not found")
			return
		}
		u.Username = newUsername
		writeJSON(w, http.StatusOK, u)
	case http.MethodDelete:
		res, err := db.Exec(`DELETE FROM users WHERE username=$1`, username)
		if err != nil {
			httpError(w, http.StatusInternalServerError, "db error")
			return
		}
		n, _ := res.RowsAffected()
		if n == 0 {
			httpError(w, http.StatusNotFound, "user not found")
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"message": "deleted"})
	default:
		w.Header().Set("Allow", "GET, PUT, DELETE")
		httpError(w, http.StatusMethodNotAllowed, "method not allowed")
	}
}

// ---------- Main ----------

func main() {
	if err := initDB(); err != nil {
		log.Fatalf("failed to init db: %v", err)
	}
	defer db.Close()

	mux := http.NewServeMux()
	mux.HandleFunc("/pet", handlePetRoot)
	mux.HandleFunc("/pet/findByStatus", findPetsByStatus)
	mux.HandleFunc("/pet/", handlePetByID)
	mux.HandleFunc("/store/order", handleOrderRoot)
	mux.HandleFunc("/store/order/", handleOrderByID)
	mux.HandleFunc("/user", handleUserRoot)
	mux.HandleFunc("/user/login", handleUserLogin)
	mux.HandleFunc("/user/", handleUserByName)

	port := getenv("PORT", "5001")
	addr := "0.0.0.0:" + port

	srv := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	log.Printf("Listening on %s", addr)
	if err := srv.ListenAndServe(); err != nil {
		log.Fatalf("server error: %v", err)
	}
}
