package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	_ "github.com/lib/pq"
)

type Pet struct {
	ID        int64    `json:"id,omitempty"`
	Name      string   `json:"name"`
	PhotoURLs []string `json:"photoUrls"`
	Status    string   `json:"status,omitempty"`
}

type Order struct {
	ID       int64  `json:"id,omitempty"`
	PetID    int64  `json:"petId,omitempty"`
	Quantity int    `json:"quantity,omitempty"`
	ShipDate string `json:"shipDate,omitempty"`
	Status   string `json:"status,omitempty"`
	Complete bool   `json:"complete,omitempty"`
}

type User struct {
	ID         int64  `json:"id,omitempty"`
	Username   string `json:"username,omitempty"`
	FirstName  string `json:"firstName,omitempty"`
	LastName   string `json:"lastName,omitempty"`
	Email      string `json:"email,omitempty"`
	Password   string `json:"password,omitempty"`
	Phone      string `json:"phone,omitempty"`
	UserStatus int    `json:"userStatus,omitempty"`
}

type App struct {
	db *sql.DB
}

func main() {
	db, err := openDB()
	if err != nil {
		log.Fatal(err)
	}
	defer db.Close()

	if err := initDB(db); err != nil {
		log.Fatal(err)
	}

	app := &App{db: db}

	mux := http.NewServeMux()
	mux.HandleFunc("/pet/findByStatus", app.handleFindPetsByStatus)
	mux.HandleFunc("/pet/", app.handlePetByID)
	mux.HandleFunc("/pet", app.handlePet)

	mux.HandleFunc("/store/order/", app.handleOrderByID)
	mux.HandleFunc("/store/order", app.handleOrder)

	mux.HandleFunc("/user/login", app.handleLoginUser)
	mux.HandleFunc("/user/", app.handleUserByUsername)
	mux.HandleFunc("/user", app.handleUser)

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	addr := net.JoinHostPort("0.0.0.0", port)
	log.Printf("listening on %s", addr)
	if err := http.ListenAndServe(addr, loggingMiddleware(mux)); err != nil {
		log.Fatal(err)
	}
}

func openDB() (*sql.DB, error) {
	host := os.Getenv("DB_HOST")
	port := os.Getenv("DB_PORT")
	user := os.Getenv("DB_USER")
	password := os.Getenv("DB_PASSWORD")
	name := os.Getenv("DB_NAME")

	if host == "" || port == "" || user == "" || name == "" {
		return nil, errors.New("database environment variables DB_HOST, DB_PORT, DB_USER, and DB_NAME must be set")
	}

	dsn := fmt.Sprintf(
		"host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, name,
	)

	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}

	db.SetMaxOpenConns(10)
	db.SetMaxIdleConns(10)
	db.SetConnMaxLifetime(time.Hour)

	return db, nil
}

func initDB(db *sql.DB) error {
	statements := []string{
		`
		CREATE TABLE IF NOT EXISTS pets (
			id BIGINT PRIMARY KEY,
			name TEXT NOT NULL,
			photo_urls TEXT[] NOT NULL,
			status TEXT,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)
		`,
		`
		CREATE TABLE IF NOT EXISTS orders (
			id BIGINT PRIMARY KEY,
			pet_id BIGINT,
			quantity INTEGER,
			ship_date TIMESTAMPTZ,
			status TEXT,
			complete BOOLEAN DEFAULT FALSE,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)
		`,
		`
		CREATE TABLE IF NOT EXISTS users (
			id BIGINT PRIMARY KEY,
			username TEXT UNIQUE NOT NULL,
			first_name TEXT,
			last_name TEXT,
			email TEXT,
			password TEXT,
			phone TEXT,
			user_status INTEGER,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)
		`,
	}

	for _, stmt := range statements {
		if _, err := db.Exec(stmt); err != nil {
			return err
		}
	}

	return nil
}

func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		log.Printf("%s %s", r.Method, r.URL.RequestURI())
		next.ServeHTTP(w, r)
	})
}

func (app *App) handlePet(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodPost:
		app.addPet(w, r)
	case http.MethodPut:
		app.updatePet(w, r)
	default:
		methodNotAllowed(w)
	}
}

func (app *App) handleFindPetsByStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w)
		return
	}
	status := r.URL.Query().Get("status")
	if !isValidPetStatus(status) {
		writeJSONError(w, http.StatusBadRequest, "invalid status")
		return
	}

	rows, err := app.db.Query(`SELECT id, name, photo_urls, COALESCE(status, '') FROM pets WHERE status = $1 ORDER BY id`, status)
	if err != nil {
		writeJSONError(w, http.StatusInternalServerError, "failed to fetch pets")
		return
	}
	defer rows.Close()

	pets := make([]Pet, 0)
	for rows.Next() {
		var p Pet
		var photoURLs []string
		if err := rows.Scan(&p.ID, &p.Name, pqStringArray(&photoURLs), &p.Status); err != nil {
			writeJSONError(w, http.StatusInternalServerError, "failed to scan pet")
			return
		}
		p.PhotoURLs = photoURLs
		pets = append(pets, p)
	}

	if err := rows.Err(); err != nil {
		writeJSONError(w, http.StatusInternalServerError, "failed to read pets")
		return
	}

	writeJSON(w, http.StatusOK, pets)
}

func (app *App) handlePetByID(w http.ResponseWriter, r *http.Request) {
	petID, ok := parseIDFromPath(r.URL.Path, "/pet/")
	if !ok {
		http.NotFound(w, r)
		return
	}

	switch r.Method {
	case http.MethodGet:
		app.getPetByID(w, r, petID)
	case http.MethodDelete:
		app.deletePet(w, r, petID)
	default:
		methodNotAllowed(w)
	}
}

func (app *App) addPet(w http.ResponseWriter, r *http.Request) {
	var p Pet
	if err := decodeJSON(r, &p); err != nil {
		writeJSONError(w, http.StatusBadRequest, "invalid input")
		return
	}
	if err := validatePet(p); err != nil {
		writeJSONError(w, http.StatusBadRequest, err.Error())
		return
	}
	if p.ID == 0 {
		p.ID = time.Now().UnixNano()
	}

	_, err := app.db.Exec(
		`INSERT INTO pets (id, name, photo_urls, status) VALUES ($1, $2, $3, $4)`,
		p.ID, p.Name, stringArrayValue(p.PhotoURLs), nullIfEmpty(p.Status),
	)
	if err != nil {
		writeJSONError(w, http.StatusBadRequest, "invalid input")
		return
	}

	writeJSON(w, http.StatusOK, p)
}

func (app *App) updatePet(w http.ResponseWriter, r *http.Request) {
	var p Pet
	if err := decodeJSON(r, &p); err != nil {
		writeJSONError(w, http.StatusBadRequest, "invalid input")
		return
	}
	if p.ID == 0 {
		writeJSONError(w, http.StatusBadRequest, "id is required")
		return
	}
	if err := validatePet(p); err != nil {
		writeJSONError(w, http.StatusBadRequest, err.Error())
		return
	}

	res, err := app.db.Exec(
		`UPDATE pets SET name = $1, photo_urls = $2, status = $3 WHERE id = $4`,
		p.Name, stringArrayValue(p.PhotoURLs), nullIfEmpty(p.Status), p.ID,
	)
	if err != nil {
		writeJSONError(w, http.StatusInternalServerError, "failed to update pet")
		return
	}
	affected, err := res.RowsAffected()
	if err != nil {
		writeJSONError(w, http.StatusInternalServerError, "failed to update pet")
		return
	}
	if affected == 0 {
		writeJSONError(w, http.StatusNotFound, "pet not found")
		return
	}

	writeJSON(w, http.StatusOK, p)
}

func (app *App) getPetByID(w http.ResponseWriter, r *http.Request, petID int64) {
	var p Pet
	var photoURLs []string
	err := app.db.QueryRow(
		`SELECT id, name, photo_urls, COALESCE(status, '') FROM pets WHERE id = $1`,
		petID,
	).Scan(&p.ID, &p.Name, pqStringArray(&photoURLs), &p.Status)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			writeJSONError(w, http.StatusNotFound, "pet not found")
			return
		}
		writeJSONError(w, http.StatusInternalServerError, "failed to fetch pet")
		return
	}
	p.PhotoURLs = photoURLs
	writeJSON(w, http.StatusOK, p)
}

func (app *App) deletePet(w http.ResponseWriter, r *http.Request, petID int64) {
	res, err := app.db.Exec(`DELETE FROM pets WHERE id = $1`, petID)
	if err != nil {
		writeJSONError(w, http.StatusInternalServerError, "failed to delete pet")
		return
	}
	affected, err := res.RowsAffected()
	if err != nil {
		writeJSONError(w, http.StatusInternalServerError, "failed to delete pet")
		return
	}
	if affected == 0 {
		writeJSONError(w, http.StatusNotFound, "pet not found")
		return
	}
	w.WriteHeader(http.StatusOK)
}

func (app *App) handleOrder(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodPost:
		app.placeOrder(w, r)
	default:
		methodNotAllowed(w)
	}
}

func (app *App) handleOrderByID(w http.ResponseWriter, r *http.Request) {
	orderID, ok := parseIDFromPath(r.URL.Path, "/store/order/")
	if !ok {
		http.NotFound(w, r)
		return
	}

	switch r.Method {
	case http.MethodGet:
		app.getOrderByID(w, r, orderID)
	case http.MethodDelete:
		app.deleteOrder(w, r, orderID)
	default:
		methodNotAllowed(w)
	}
}

func (app *App) placeOrder(w http.ResponseWriter, r *http.Request) {
	var o Order
	if err := decodeJSON(r, &o); err != nil {
		writeJSONError(w, http.StatusBadRequest, "invalid input")
		return
	}
	if o.ID == 0 {
		o.ID = time.Now().UnixNano()
	}
	if o.Status != "" && !isValidOrderStatus(o.Status) {
		writeJSONError(w, http.StatusBadRequest, "invalid order status")
		return
	}
	if o.ShipDate != "" {
		if _, err := time.Parse(time.RFC3339, o.ShipDate); err != nil {
			writeJSONError(w, http.StatusBadRequest, "invalid shipDate")
			return
		}
	}

	_, err := app.db.Exec(
		`INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete) VALUES ($1, $2, $3, $4, $5, $6)`,
		o.ID, nullableInt64(o.PetID), nullableInt(o.Quantity), nullableTime(o.ShipDate), nullIfEmpty(o.Status), o.Complete,
	)
	if err != nil {
		writeJSONError(w, http.StatusInternalServerError, "failed to place order")
		return
	}

	writeJSON(w, http.StatusOK, o)
}

func (app *App) getOrderByID(w http.ResponseWriter, r *http.Request, orderID int64) {
	var o Order
	var shipDate sql.NullTime
	var petID sql.NullInt64
	var quantity sql.NullInt64
	var status sql.NullString

	err := app.db.QueryRow(
		`SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id = $1`,
		orderID,
	).Scan(&o.ID, &petID, &quantity, &shipDate, &status, &o.Complete)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			writeJSONError(w, http.StatusNotFound, "order not found")
			return
		}
		writeJSONError(w, http.StatusInternalServerError, "failed to fetch order")
		return
	}

	if petID.Valid {
		o.PetID = petID.Int64
	}
	if quantity.Valid {
		o.Quantity = int(quantity.Int64)
	}
	if shipDate.Valid {
		o.ShipDate = shipDate.Time.UTC().Format(time.RFC3339)
	}
	if status.Valid {
		o.Status = status.String
	}

	writeJSON(w, http.StatusOK, o)
}

func (app *App) deleteOrder(w http.ResponseWriter, r *http.Request, orderID int64) {
	res, err := app.db.Exec(`DELETE FROM orders WHERE id = $1`, orderID)
	if err != nil {
		writeJSONError(w, http.StatusInternalServerError, "failed to delete order")
		return
	}
	affected, err := res.RowsAffected()
	if err != nil {
		writeJSONError(w, http.StatusInternalServerError, "failed to delete order")
		return
	}
	if affected == 0 {
		writeJSONError(w, http.StatusNotFound, "order not found")
		return
	}
	w.WriteHeader(http.StatusOK)
}

func (app *App) handleUser(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodPost:
		app.createUser(w, r)
	default:
		methodNotAllowed(w)
	}
}

func (app *App) handleUserByUsername(w http.ResponseWriter, r *http.Request) {
	username := strings.TrimPrefix(r.URL.Path, "/user/")
	if username == "" || strings.Contains(username, "/") {
		http.NotFound(w, r)
		return
	}

	switch r.Method {
	case http.MethodGet:
		app.getUserByName(w, r, username)
	case http.MethodPut:
		app.updateUser(w, r, username)
	case http.MethodDelete:
		app.deleteUser(w, r, username)
	default:
		methodNotAllowed(w)
	}
}

func (app *App) handleLoginUser(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w)
		return
	}

	username := r.URL.Query().Get("username")
	password := r.URL.Query().Get("password")
	if username == "" || password == "" {
		writeJSONError(w, http.StatusBadRequest, "invalid credentials")
		return
	}

	var storedPassword string
	err := app.db.QueryRow(`SELECT password FROM users WHERE username = $1`, username).Scan(&storedPassword)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			writeJSONError(w, http.StatusBadRequest, "invalid credentials")
			return
		}
		writeJSONError(w, http.StatusInternalServerError, "failed to login")
		return
	}

	if storedPassword != password {
		writeJSONError(w, http.StatusBadRequest, "invalid credentials")
		return
	}

	writeJSON(w, http.StatusOK, "logged in")
}

func (app *App) createUser(w http.ResponseWriter, r *http.Request) {
	var u User
	if err := decodeJSON(r, &u); err != nil {
		writeJSONError(w, http.StatusBadRequest, "invalid input")
		return
	}
	if strings.TrimSpace(u.Username) == "" {
		writeJSONError(w, http.StatusBadRequest, "username is required")
		return
	}
	if u.ID == 0 {
		u.ID = time.Now().UnixNano()
	}

	_, err := app.db.Exec(
		`INSERT INTO users (id, username, first_name, last_name, email, password, phone, user_status)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
		u.ID, u.Username, nullIfEmpty(u.FirstName), nullIfEmpty(u.LastName), nullIfEmpty(u.Email),
		nullIfEmpty(u.Password), nullIfEmpty(u.Phone), nullableInt(u.UserStatus),
	)
	if err != nil {
		writeJSONError(w, http.StatusBadRequest, "invalid input")
		return
	}

	writeJSON(w, http.StatusOK, u)
}

func (app *App) getUserByName(w http.ResponseWriter, r *http.Request, username string) {
	var u User
	var firstName, lastName, email, password, phone sql.NullString
	var userStatus sql.NullInt64

	err := app.db.QueryRow(
		`SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username = $1`,
		username,
	).Scan(&u.ID, &u.Username, &firstName, &lastName, &email, &password, &phone, &userStatus)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			writeJSONError(w, http.StatusNotFound, "user not found")
			return
		}
		writeJSONError(w, http.StatusInternalServerError, "failed to fetch user")
		return
	}

	if firstName.Valid {
		u.FirstName = firstName.String
	}
	if lastName.Valid {
		u.LastName = lastName.String
	}
	if email.Valid {
		u.Email = email.String
	}
	if password.Valid {
		u.Password = password.String
	}
	if phone.Valid {
		u.Phone = phone.String
	}
	if userStatus.Valid {
		u.UserStatus = int(userStatus.Int64)
	}

	writeJSON(w, http.StatusOK, u)
}

func (app *App) updateUser(w http.ResponseWriter, r *http.Request, username string) {
	var u User
	if err := decodeJSON(r, &u); err != nil {
		writeJSONError(w, http.StatusBadRequest, "invalid input")
		return
	}
	if strings.TrimSpace(u.Username) == "" {
		u.Username = username
	}

	res, err := app.db.Exec(
		`UPDATE users
		 SET username = $1, first_name = $2, last_name = $3, email = $4, password = $5, phone = $6, user_status = $7
		 WHERE username = $8`,
		u.Username, nullIfEmpty(u.FirstName), nullIfEmpty(u.LastName), nullIfEmpty(u.Email),
		nullIfEmpty(u.Password), nullIfEmpty(u.Phone), nullableInt(u.UserStatus), username,
	)
	if err != nil {
		writeJSONError(w, http.StatusInternalServerError, "failed to update user")
		return
	}
	affected, err := res.RowsAffected()
	if err != nil {
		writeJSONError(w, http.StatusInternalServerError, "failed to update user")
		return
	}
	if affected == 0 {
		writeJSONError(w, http.StatusNotFound, "user not found")
		return
	}

	var id int64
	err = app.db.QueryRow(`SELECT id FROM users WHERE username = $1`, u.Username).Scan(&id)
	if err == nil {
		u.ID = id
	}

	writeJSON(w, http.StatusOK, u)
}

func (app *App) deleteUser(w http.ResponseWriter, r *http.Request, username string) {
	res, err := app.db.Exec(`DELETE FROM users WHERE username = $1`, username)
	if err != nil {
		writeJSONError(w, http.StatusInternalServerError, "failed to delete user")
		return
	}
	affected, err := res.RowsAffected()
	if err != nil {
		writeJSONError(w, http.StatusInternalServerError, "failed to delete user")
		return
	}
	if affected == 0 {
		writeJSONError(w, http.StatusNotFound, "user not found")
		return
	}
	w.WriteHeader(http.StatusOK)
}

func validatePet(p Pet) error {
	if strings.TrimSpace(p.Name) == "" {
		return errors.New("name is required")
	}
	if len(p.PhotoURLs) == 0 {
		return errors.New("photoUrls is required")
	}
	if p.Status != "" && !isValidPetStatus(p.Status) {
		return errors.New("invalid status")
	}
	return nil
}

func isValidPetStatus(status string) bool {
	switch status {
	case "available", "pending", "sold":
		return true
	default:
		return false
	}
}

func isValidOrderStatus(status string) bool {
	switch status {
	case "placed", "approved", "delivered":
		return true
	default:
		return false
	}
}

func parseIDFromPath(path, prefix string) (int64, bool) {
	raw := strings.TrimPrefix(path, prefix)
	if raw == "" || strings.Contains(raw, "/") {
		return 0, false
	}
	id, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		return 0, false
	}
	return id, true
}

func decodeJSON(r *http.Request, dst any) error {
	defer r.Body.Close()
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	return dec.Decode(dst)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeJSONError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}

func methodNotAllowed(w http.ResponseWriter) {
	w.WriteHeader(http.StatusMethodNotAllowed)
}

func nullIfEmpty(s string) any {
	if strings.TrimSpace(s) == "" {
		return nil
	}
	return s
}

func nullableInt(v int) any {
	if v == 0 {
		return nil
	}
	return v
}

func nullableInt64(v int64) any {
	if v == 0 {
		return nil
	}
	return v
}

func nullableTime(v string) any {
	if strings.TrimSpace(v) == "" {
		return nil
	}
	t, err := time.Parse(time.RFC3339, v)
	if err != nil {
		return nil
	}
	return t
}

type stringArray []string

func stringArrayValue(v []string) any {
	return stringArray(v)
}

func (a stringArray) Value() (driverValue any, err error) {
	if a == nil {
		return []byte("{}"), nil
	}
	escaped := make([]string, 0, len(a))
	for _, s := range a {
		s = strings.ReplaceAll(s, `\`, `\\`)
		s = strings.ReplaceAll(s, `"`, `\"`)
		escaped = append(escaped, `"`+s+`"`)
	}
	return "{" + strings.Join(escaped, ",") + "}", nil
}

type pqStringArray []string

func (a pqStringArray) Scan(src any) error {
	switch v := src.(type) {
	case []byte:
		return scanTextArray(string(v), []string(a))
	case string:
		return scanTextArray(v, []string(a))
	default:
		return fmt.Errorf("unsupported array source type %T", src)
	}
}

func scanTextArray(input string, dst []string) error {
	if len(input) < 2 || input[0] != '{' || input[len(input)-1] != '}' {
		return fmt.Errorf("invalid postgres text array")
	}
	content := input[1 : len(input)-1]
	if content == "" {
		return nil
	}

	var result []string
	var cur strings.Builder
	inQuotes := false
	escaped := false

	for i := 0; i < len(content); i++ {
		ch := content[i]

		if escaped {
			cur.WriteByte(ch)
			escaped = false
			continue
		}
		if ch == '\\' {
			escaped = true
			continue
		}
		if ch == '"' {
			inQuotes = !inQuotes
			continue
		}
		if ch == ',' && !inQuotes {
			result = append(result, cur.String())
			cur.Reset()
			continue
		}
		cur.WriteByte(ch)
	}
	result = append(result, cur.String())

	ptr := any(&dst).(*[]string)
	*ptr = result
	return nil
}