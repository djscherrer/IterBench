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
	"sync"
	"time"

	_ "github.com/lib/pq"
)

// Models

type Pet struct {
	ID        int64    `json:"id"`
	Name      string   `json:"name"`
	PhotoUrls []string `json:"photoUrls"`
	Status    string   `json:"status,omitempty"`
}

type Order struct {
	ID       int64  `json:"id"`
	PetID    int64  `json:"petId,omitempty"`
	Quantity int    `json:"quantity,omitempty"`
	ShipDate string `json:"shipDate,omitempty"`
	Status   string `json:"status,omitempty"`
	Complete bool   `json:"complete,omitempty"`
}

type User struct {
	ID         int64  `json:"id"`
	Username   string `json:"username,omitempty"`
	FirstName  string `json:"firstName,omitempty"`
	LastName   string `json:"lastName,omitempty"`
	Email      string `json:"email,omitempty"`
	Password   string `json:"password,omitempty"`
	Phone      string `json:"phone,omitempty"`
	UserStatus int    `json:"userStatus,omitempty"`
}

var (
	db   *sql.DB
	dbMu sync.Mutex
)

func initDB() {
	host := getEnv("DB_HOST", "localhost")
	port := getEnv("DB_PORT", "5432")
	user := getEnv("DB_USER", "postgres")
	password := getEnv("DB_PASSWORD", "postgres")
	dbname := getEnv("DB_NAME", "petstore")

	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, dbname)

	var err error
	db, err = sql.Open("postgres", connStr)
	if err != nil {
		log.Fatalf("Failed to open database: %v", err)
	}

	// Retry connection
	for i := 0; i < 30; i++ {
		err = db.Ping()
		if err == nil {
			break
		}
		log.Printf("Waiting for database... (%v)", err)
		time.Sleep(1 * time.Second)
	}
	if err != nil {
		log.Fatalf("Failed to connect to database: %v", err)
	}

	createTables()
}

func createTables() {
	queries := []string{
		`CREATE TABLE IF NOT EXISTS pets (
			id BIGSERIAL PRIMARY KEY,
			name TEXT NOT NULL,
			photo_urls TEXT[] NOT NULL DEFAULT '{}',
			status TEXT DEFAULT 'available'
		)`,
		`CREATE TABLE IF NOT EXISTS orders (
			id BIGSERIAL PRIMARY KEY,
			pet_id BIGINT DEFAULT 0,
			quantity INT DEFAULT 0,
			ship_date TEXT DEFAULT '',
			status TEXT DEFAULT '',
			complete BOOLEAN DEFAULT false
		)`,
		`CREATE TABLE IF NOT EXISTS users (
			id BIGSERIAL PRIMARY KEY,
			username TEXT UNIQUE,
			first_name TEXT DEFAULT '',
			last_name TEXT DEFAULT '',
			email TEXT DEFAULT '',
			password TEXT DEFAULT '',
			phone TEXT DEFAULT '',
			user_status INT DEFAULT 0
		)`,
	}

	for _, q := range queries {
		_, err := db.Exec(q)
		if err != nil {
			log.Fatalf("Failed to create table: %v\nQuery: %s", err, q)
		}
	}
}

func getEnv(key, fallback string) string {
	if val, ok := os.LookupEnv(key); ok {
		return val
	}
	return fallback
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(map[string]string{"message": msg})
}

// Pet handlers

func addPet(w http.ResponseWriter, r *http.Request) {
	var pet Pet
	if err := json.NewDecoder(r.Body).Decode(&pet); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}
	if pet.Name == "" {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}
	if pet.PhotoUrls == nil {
		pet.PhotoUrls = []string{}
	}

	dbMu.Lock()
	defer dbMu.Unlock()

	if pet.ID != 0 {
		err := db.QueryRow(
			`INSERT INTO pets (id, name, photo_urls, status) VALUES ($1, $2, $3, $4) RETURNING id`,
			pet.ID, pet.Name, pqStringArray(pet.PhotoUrls), pet.Status,
		).Scan(&pet.ID)
		if err != nil {
			// Try without specifying ID if conflict
			err = db.QueryRow(
				`INSERT INTO pets (name, photo_urls, status) VALUES ($1, $2, $3) RETURNING id`,
				pet.Name, pqStringArray(pet.PhotoUrls), pet.Status,
			).Scan(&pet.ID)
			if err != nil {
				writeError(w, http.StatusBadRequest, "Invalid input")
				return
			}
		}
	} else {
		err := db.QueryRow(
			`INSERT INTO pets (name, photo_urls, status) VALUES ($1, $2, $3) RETURNING id`,
			pet.Name, pqStringArray(pet.PhotoUrls), pet.Status,
		).Scan(&pet.ID)
		if err != nil {
			writeError(w, http.StatusBadRequest, "Invalid input")
			return
		}
	}

	writeJSON(w, http.StatusOK, pet)
}

func updatePet(w http.ResponseWriter, r *http.Request) {
	var pet Pet
	if err := json.NewDecoder(r.Body).Decode(&pet); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}
	if pet.PhotoUrls == nil {
		pet.PhotoUrls = []string{}
	}

	dbMu.Lock()
	defer dbMu.Unlock()

	result, err := db.Exec(
		`UPDATE pets SET name=$1, photo_urls=$2, status=$3 WHERE id=$4`,
		pet.Name, pqStringArray(pet.PhotoUrls), pet.Status, pet.ID,
	)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}
	rows, _ := result.RowsAffected()
	if rows == 0 {
		writeError(w, http.StatusNotFound, "Pet not found")
		return
	}

	writeJSON(w, http.StatusOK, pet)
}

func findPetsByStatus(w http.ResponseWriter, r *http.Request) {
	status := r.URL.Query().Get("status")
	if status == "" {
		writeJSON(w, http.StatusOK, []Pet{})
		return
	}

	rows, err := db.Query(`SELECT id, name, photo_urls, status FROM pets WHERE status=$1`, status)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Internal error")
		return
	}
	defer rows.Close()

	pets := []Pet{}
	for rows.Next() {
		var p Pet
		var urls pqStringArrayScanner
		if err := rows.Scan(&p.ID, &p.Name, &urls, &p.Status); err != nil {
			writeError(w, http.StatusInternalServerError, "Internal error")
			return
		}
		p.PhotoUrls = []string(urls)
		if p.PhotoUrls == nil {
			p.PhotoUrls = []string{}
		}
		pets = append(pets, p)
	}

	writeJSON(w, http.StatusOK, pets)
}

func getPetById(w http.ResponseWriter, r *http.Request, idStr string) {
	id, err := strconv.ParseInt(idStr, 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid ID")
		return
	}

	var p Pet
	var urls pqStringArrayScanner
	err = db.QueryRow(`SELECT id, name, photo_urls, status FROM pets WHERE id=$1`, id).
		Scan(&p.ID, &p.Name, &urls, &p.Status)
	if err != nil {
		writeError(w, http.StatusNotFound, "Pet not found")
		return
	}
	p.PhotoUrls = []string(urls)
	if p.PhotoUrls == nil {
		p.PhotoUrls = []string{}
	}

	writeJSON(w, http.StatusOK, p)
}

func deletePet(w http.ResponseWriter, r *http.Request, idStr string) {
	id, err := strconv.ParseInt(idStr, 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid ID")
		return
	}

	dbMu.Lock()
	defer dbMu.Unlock()

	result, err := db.Exec(`DELETE FROM pets WHERE id=$1`, id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Internal error")
		return
	}
	rows, _ := result.RowsAffected()
	if rows == 0 {
		writeError(w, http.StatusNotFound, "Pet not found")
		return
	}

	writeJSON(w, http.StatusOK, map[string]string{"message": "successful operation"})
}

// Order handlers

func placeOrder(w http.ResponseWriter, r *http.Request) {
	var order Order
	if err := json.NewDecoder(r.Body).Decode(&order); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	dbMu.Lock()
	defer dbMu.Unlock()

	if order.ID != 0 {
		err := db.QueryRow(
			`INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete) VALUES ($1,$2,$3,$4,$5,$6) RETURNING id`,
			order.ID, order.PetID, order.Quantity, order.ShipDate, order.Status, order.Complete,
		).Scan(&order.ID)
		if err != nil {
			err = db.QueryRow(
				`INSERT INTO orders (pet_id, quantity, ship_date, status, complete) VALUES ($1,$2,$3,$4,$5) RETURNING id`,
				order.PetID, order.Quantity, order.ShipDate, order.Status, order.Complete,
			).Scan(&order.ID)
			if err != nil {
				writeError(w, http.StatusBadRequest, "Invalid input")
				return
			}
		}
	} else {
		err := db.QueryRow(
			`INSERT INTO orders (pet_id, quantity, ship_date, status, complete) VALUES ($1,$2,$3,$4,$5) RETURNING id`,
			order.PetID, order.Quantity, order.ShipDate, order.Status, order.Complete,
		).Scan(&order.ID)
		if err != nil {
			writeError(w, http.StatusBadRequest, "Invalid input")
			return
		}
	}

	writeJSON(w, http.StatusOK, order)
}

func getOrderById(w http.ResponseWriter, r *http.Request, idStr string) {
	id, err := strconv.ParseInt(idStr, 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid ID")
		return
	}

	var o Order
	err = db.QueryRow(`SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id=$1`, id).
		Scan(&o.ID, &o.PetID, &o.Quantity, &o.ShipDate, &o.Status, &o.Complete)
	if err != nil {
		writeError(w, http.StatusNotFound, "Order not found")
		return
	}

	writeJSON(w, http.StatusOK, o)
}

func deleteOrder(w http.ResponseWriter, r *http.Request, idStr string) {
	id, err := strconv.ParseInt(idStr, 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid ID")
		return
	}

	dbMu.Lock()
	defer dbMu.Unlock()

	result, err := db.Exec(`DELETE FROM orders WHERE id=$1`, id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Internal error")
		return
	}
	rows, _ := result.RowsAffected()
	if rows == 0 {
		writeError(w, http.StatusNotFound, "Order not found")
		return
	}

	writeJSON(w, http.StatusOK, map[string]string{"message": "successful operation"})
}

// User handlers

func createUser(w http.ResponseWriter, r *http.Request) {
	var user User
	if err := json.NewDecoder(r.Body).Decode(&user); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	dbMu.Lock()
	defer dbMu.Unlock()

	if user.ID != 0 {
		err := db.QueryRow(
			`INSERT INTO users (id, username, first_name, last_name, email, password, phone, user_status)
			 VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id`,
			user.ID, user.Username, user.FirstName, user.LastName, user.Email, user.Password, user.Phone, user.UserStatus,
		).Scan(&user.ID)
		if err != nil {
			err = db.QueryRow(
				`INSERT INTO users (username, first_name, last_name, email, password, phone, user_status)
				 VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id`,
				user.Username, user.FirstName, user.LastName, user.Email, user.Password, user.Phone, user.UserStatus,
			).Scan(&user.ID)
			if err != nil {
				writeError(w, http.StatusBadRequest, "Invalid input")
				return
			}
		}
	} else {
		err := db.QueryRow(
			`INSERT INTO users (username, first_name, last_name, email, password, phone, user_status)
			 VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id`,
			user.Username, user.FirstName, user.LastName, user.Email, user.Password, user.Phone, user.UserStatus,
		).Scan(&user.ID)
		if err != nil {
			writeError(w, http.StatusBadRequest, "Invalid input")
			return
		}
	}

	writeJSON(w, http.StatusOK, user)
}

func getUserByName(w http.ResponseWriter, r *http.Request, username string) {
	var u User
	err := db.QueryRow(
		`SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username=$1`,
		username,
	).Scan(&u.ID, &u.Username, &u.FirstName, &u.LastName, &u.Email, &u.Password, &u.Phone, &u.UserStatus)
	if err != nil {
		writeError(w, http.StatusNotFound, "User not found")
		return
	}

	writeJSON(w, http.StatusOK, u)
}

func updateUser(w http.ResponseWriter, r *http.Request, username string) {
	var user User
	if err := json.NewDecoder(r.Body).Decode(&user); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	dbMu.Lock()
	defer dbMu.Unlock()

	// Check if user exists
	var existingID int64
	err := db.QueryRow(`SELECT id FROM users WHERE username=$1`, username).Scan(&existingID)
	if err != nil {
		writeError(w, http.StatusNotFound, "User not found")
		return
	}

	if user.ID == 0 {
		user.ID = existingID
	}

	_, err = db.Exec(
		`UPDATE users SET username=$1, first_name=$2, last_name=$3, email=$4, password=$5, phone=$6, user_status=$7 WHERE id=$8`,
		user.Username, user.FirstName, user.LastName, user.Email, user.Password, user.Phone, user.UserStatus, existingID,
	)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	user.ID = existingID
	writeJSON(w, http.StatusOK, user)
}

func deleteUser(w http.ResponseWriter, r *http.Request, username string) {
	dbMu.Lock()
	defer dbMu.Unlock()

	result, err := db.Exec(`DELETE FROM users WHERE username=$1`, username)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Internal error")
		return
	}
	rows, _ := result.RowsAffected()
	if rows == 0 {
		writeError(w, http.StatusNotFound, "User not found")
		return
	}

	writeJSON(w, http.StatusOK, map[string]string{"message": "successful operation"})
}

func loginUser(w http.ResponseWriter, r *http.Request) {
	username := r.URL.Query().Get("username")
	password := r.URL.Query().Get("password")

	if username == "" || password == "" {
		writeError(w, http.StatusBadRequest, "Invalid credentials")
		return
	}

	var storedPassword string
	err := db.QueryRow(`SELECT password FROM users WHERE username=$1`, username).Scan(&storedPassword)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid credentials")
		return
	}

	if storedPassword != password {
		writeError(w, http.StatusBadRequest, "Invalid credentials")
		return
	}

	token := fmt.Sprintf("logged in user session: %s", username)
	writeJSON(w, http.StatusOK, token)
}

// PostgreSQL array helpers

type pqStringArray []string

func (a pqStringArray) Value() (interface{}, error) {
	if a == nil {
		return "{}", nil
	}
	elements := make([]string, len(a))
	for i, s := range a {
		escaped := strings.ReplaceAll(s, `\`, `\\`)
		escaped = strings.ReplaceAll(escaped, `"`, `\"`)
		elements[i] = `"` + escaped + `"`
	}
	return "{" + strings.Join(elements, ",") + "}", nil
}

type pqStringArrayScanner []string

func (a *pqStringArrayScanner) Scan(src interface{}) error {
	if src == nil {
		*a = []string{}
		return nil
	}

	var s string
	switch v := src.(type) {
	case []byte:
		s = string(v)
	case string:
		s = v
	default:
		return fmt.Errorf("unsupported type: %T", src)
	}

	// Parse PostgreSQL array format: {elem1,elem2,...}
	s = strings.TrimSpace(s)
	if s == "{}" || s == "" {
		*a = []string{}
		return nil
	}

	// Remove outer braces
	s = s[1 : len(s)-1]

	result := parsePostgresArray(s)
	*a = result
	return nil
}

func parsePostgresArray(s string) []string {
	var result []string
	var current strings.Builder
	inQuotes := false
	escaped := false

	for i := 0; i < len(s); i++ {
		c := s[i]
		if escaped {
			current.WriteByte(c)
			escaped = false
			continue
		}
		if c == '\\' {
			escaped = true
			continue
		}
		if c == '"' {
			inQuotes = !inQuotes
			continue
		}
		if c == ',' && !inQuotes {
			result = append(result, current.String())
			current.Reset()
			continue
		}
		current.WriteByte(c)
	}
	result = append(result, current.String())
	return result
}

// Router

func main() {
	initDB()

	mux := http.NewServeMux()

	mux.HandleFunc("/pet/findByStatus", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			findPetsByStatus(w, r)
		} else {
			w.WriteHeader(http.StatusMethodNotAllowed)
		}
	})

	mux.HandleFunc("/pet/", func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		// /pet/{petId}
		parts := strings.Split(strings.TrimPrefix(path, "/pet/"), "/")
		if len(parts) == 1 && parts[0] != "" {
			petId := parts[0]
			switch r.Method {
			case http.MethodGet:
				getPetById(w, r, petId)
			case http.MethodDelete:
				deletePet(w, r, petId)
			default:
				w.WriteHeader(http.StatusMethodNotAllowed)
			}
			return
		}
		w.WriteHeader(http.StatusNotFound)
	})

	mux.HandleFunc("/pet", func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodPost:
			addPet(w, r)
		case http.MethodPut:
			updatePet(w, r)
		default:
			w.WriteHeader(http.StatusMethodNotAllowed)
		}
	})

	mux.HandleFunc("/store/order/", func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		parts := strings.Split(strings.TrimPrefix(path, "/store/order/"), "/")
		if len(parts) == 1 && parts[0] != "" {
			orderId := parts[0]
			switch r.Method {
			case http.MethodGet:
				getOrderById(w, r, orderId)
			case http.MethodDelete:
				deleteOrder(w, r, orderId)
			default:
				w.WriteHeader(http.StatusMethodNotAllowed)
			}
			return
		}
		w.WriteHeader(http.StatusNotFound)
	})

	mux.HandleFunc("/store/order", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			placeOrder(w, r)
		} else {
			w.WriteHeader(http.StatusMethodNotAllowed)
		}
	})

	mux.HandleFunc("/user/login", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			loginUser(w, r)
		} else {
			w.WriteHeader(http.StatusMethodNotAllowed)
		}
	})

	mux.HandleFunc("/user/", func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		username := strings.TrimPrefix(path, "/user/")
		if username == "" || strings.Contains(username, "/") {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		// Make sure we don't catch /user/login here
		if username == "login" {
			if r.Method == http.MethodGet {
				loginUser(w, r)
			} else {
				w.WriteHeader(http.StatusMethodNotAllowed)
			}
			return
		}
		switch r.Method {
		case http.MethodGet:
			getUserByName(w, r, username)
		case http.MethodPut:
			updateUser(w, r, username)
		case http.MethodDelete:
			deleteUser(w, r, username)
		default:
			w.WriteHeader(http.StatusMethodNotAllowed)
		}
	})

	mux.HandleFunc("/user", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			createUser(w, r)
		} else {
			w.WriteHeader(http.StatusMethodNotAllowed)
		}
	})

	port := getEnv("PORT", "5001")
	addr := "0.0.0.0:" + port
	log.Printf("Starting server on %s", addr)
	log.Fatal(http.ListenAndServe(addr, mux))
}