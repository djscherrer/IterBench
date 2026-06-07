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
	ID        int64    `json:"id,omitempty"`
	Name      string   `json:"name"`
	PhotoUrls []string `json:"photoUrls"`
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
			name VARCHAR(255) NOT NULL,
			photo_urls TEXT[] NOT NULL DEFAULT '{}',
			status VARCHAR(50) DEFAULT 'available'
		)`,
		`CREATE TABLE IF NOT EXISTS orders (
			id BIGSERIAL PRIMARY KEY,
			pet_id BIGINT DEFAULT 0,
			quantity INT DEFAULT 0,
			ship_date TIMESTAMPTZ,
			status VARCHAR(50) DEFAULT 'placed',
			complete BOOLEAN DEFAULT false
		)`,
		`CREATE TABLE IF NOT EXISTS users (
			id BIGSERIAL PRIMARY KEY,
			username VARCHAR(255) UNIQUE,
			first_name VARCHAR(255) DEFAULT '',
			last_name VARCHAR(255) DEFAULT '',
			email VARCHAR(255) DEFAULT '',
			password VARCHAR(255) DEFAULT '',
			phone VARCHAR(255) DEFAULT '',
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

func writeJSON(w http.ResponseWriter, status int, data interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(data)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(map[string]string{"message": msg})
}

// Pet handlers

func handlePet(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodPost:
		addPet(w, r)
	case http.MethodPut:
		updatePet(w, r)
	default:
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
	}
}

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

	if pet.Status == "" {
		pet.Status = "available"
	}

	dbMu.Lock()
	defer dbMu.Unlock()

	err := db.QueryRow(
		`INSERT INTO pets (name, photo_urls, status) VALUES ($1, $2, $3) RETURNING id`,
		pet.Name, pqStringArray(pet.PhotoUrls), pet.Status,
	).Scan(&pet.ID)

	if err != nil {
		log.Printf("Error adding pet: %v", err)
		writeError(w, http.StatusInternalServerError, "Internal server error")
		return
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

	// Check if pet exists
	var exists bool
	err := db.QueryRow(`SELECT EXISTS(SELECT 1 FROM pets WHERE id=$1)`, pet.ID).Scan(&exists)
	if err != nil || !exists {
		writeError(w, http.StatusNotFound, "Pet not found")
		return
	}

	_, err = db.Exec(
		`UPDATE pets SET name=$1, photo_urls=$2, status=$3 WHERE id=$4`,
		pet.Name, pqStringArray(pet.PhotoUrls), pet.Status, pet.ID,
	)
	if err != nil {
		log.Printf("Error updating pet: %v", err)
		writeError(w, http.StatusInternalServerError, "Internal server error")
		return
	}

	writeJSON(w, http.StatusOK, pet)
}

func handleFindPetsByStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	status := r.URL.Query().Get("status")
	if status == "" {
		writeJSON(w, http.StatusOK, []Pet{})
		return
	}

	rows, err := db.Query(`SELECT id, name, photo_urls, status FROM pets WHERE status=$1`, status)
	if err != nil {
		log.Printf("Error finding pets: %v", err)
		writeError(w, http.StatusInternalServerError, "Internal server error")
		return
	}
	defer rows.Close()

	pets := []Pet{}
	for rows.Next() {
		var pet Pet
		var photoUrls pqStringArrayScanner
		if err := rows.Scan(&pet.ID, &pet.Name, &photoUrls, &pet.Status); err != nil {
			log.Printf("Error scanning pet: %v", err)
			continue
		}
		pet.PhotoUrls = []string(photoUrls)
		if pet.PhotoUrls == nil {
			pet.PhotoUrls = []string{}
		}
		pets = append(pets, pet)
	}

	writeJSON(w, http.StatusOK, pets)
}

func handlePetByID(w http.ResponseWriter, r *http.Request) {
	// Extract petId from path
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/pet/"), "/")
	petIDStr := parts[0]
	petID, err := strconv.ParseInt(petIDStr, 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid pet ID")
		return
	}

	switch r.Method {
	case http.MethodGet:
		getPetByID(w, petID)
	case http.MethodDelete:
		deletePet(w, petID)
	default:
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
	}
}

func getPetByID(w http.ResponseWriter, petID int64) {
	var pet Pet
	var photoUrls pqStringArrayScanner
	err := db.QueryRow(`SELECT id, name, photo_urls, status FROM pets WHERE id=$1`, petID).
		Scan(&pet.ID, &pet.Name, &photoUrls, &pet.Status)
	if err == sql.ErrNoRows {
		writeError(w, http.StatusNotFound, "Pet not found")
		return
	}
	if err != nil {
		log.Printf("Error getting pet: %v", err)
		writeError(w, http.StatusInternalServerError, "Internal server error")
		return
	}
	pet.PhotoUrls = []string(photoUrls)
	if pet.PhotoUrls == nil {
		pet.PhotoUrls = []string{}
	}
	writeJSON(w, http.StatusOK, pet)
}

func deletePet(w http.ResponseWriter, petID int64) {
	dbMu.Lock()
	defer dbMu.Unlock()

	result, err := db.Exec(`DELETE FROM pets WHERE id=$1`, petID)
	if err != nil {
		log.Printf("Error deleting pet: %v", err)
		writeError(w, http.StatusInternalServerError, "Internal server error")
		return
	}
	rowsAffected, _ := result.RowsAffected()
	if rowsAffected == 0 {
		writeError(w, http.StatusNotFound, "Pet not found")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"message": "successful operation"})
}

// Order handlers

func handleStoreOrder(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	placeOrder(w, r)
}

func placeOrder(w http.ResponseWriter, r *http.Request) {
	var order Order
	if err := json.NewDecoder(r.Body).Decode(&order); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	if order.Status == "" {
		order.Status = "placed"
	}

	dbMu.Lock()
	defer dbMu.Unlock()

	var shipDate *time.Time
	if order.ShipDate != "" {
		t, err := time.Parse(time.RFC3339, order.ShipDate)
		if err != nil {
			// Try other formats
			t, err = time.Parse("2006-01-02T15:04:05Z", order.ShipDate)
			if err != nil {
				shipDate = nil
			} else {
				shipDate = &t
			}
		} else {
			shipDate = &t
		}
	}

	err := db.QueryRow(
		`INSERT INTO orders (pet_id, quantity, ship_date, status, complete) VALUES ($1, $2, $3, $4, $5) RETURNING id`,
		order.PetID, order.Quantity, shipDate, order.Status, order.Complete,
	).Scan(&order.ID)

	if err != nil {
		log.Printf("Error placing order: %v", err)
		writeError(w, http.StatusInternalServerError, "Internal server error")
		return
	}

	writeJSON(w, http.StatusOK, order)
}

func handleOrderByID(w http.ResponseWriter, r *http.Request) {
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/store/order/"), "/")
	orderIDStr := parts[0]
	orderID, err := strconv.ParseInt(orderIDStr, 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid order ID")
		return
	}

	switch r.Method {
	case http.MethodGet:
		getOrderByID(w, orderID)
	case http.MethodDelete:
		deleteOrder(w, orderID)
	default:
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
	}
}

func getOrderByID(w http.ResponseWriter, orderID int64) {
	var order Order
	var shipDate *time.Time
	err := db.QueryRow(
		`SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id=$1`, orderID,
	).Scan(&order.ID, &order.PetID, &order.Quantity, &shipDate, &order.Status, &order.Complete)
	if err == sql.ErrNoRows {
		writeError(w, http.StatusNotFound, "Order not found")
		return
	}
	if err != nil {
		log.Printf("Error getting order: %v", err)
		writeError(w, http.StatusInternalServerError, "Internal server error")
		return
	}
	if shipDate != nil {
		order.ShipDate = shipDate.Format(time.RFC3339)
	}
	writeJSON(w, http.StatusOK, order)
}

func deleteOrder(w http.ResponseWriter, orderID int64) {
	dbMu.Lock()
	defer dbMu.Unlock()

	result, err := db.Exec(`DELETE FROM orders WHERE id=$1`, orderID)
	if err != nil {
		log.Printf("Error deleting order: %v", err)
		writeError(w, http.StatusInternalServerError, "Internal server error")
		return
	}
	rowsAffected, _ := result.RowsAffected()
	if rowsAffected == 0 {
		writeError(w, http.StatusNotFound, "Order not found")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"message": "successful operation"})
}

// User handlers

func handleUser(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	createUser(w, r)
}

func createUser(w http.ResponseWriter, r *http.Request) {
	var user User
	if err := json.NewDecoder(r.Body).Decode(&user); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	dbMu.Lock()
	defer dbMu.Unlock()

	err := db.QueryRow(
		`INSERT INTO users (username, first_name, last_name, email, password, phone, user_status) 
		 VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id`,
		user.Username, user.FirstName, user.LastName, user.Email, user.Password, user.Phone, user.UserStatus,
	).Scan(&user.ID)

	if err != nil {
		log.Printf("Error creating user: %v", err)
		writeError(w, http.StatusInternalServerError, "Internal server error")
		return
	}

	writeJSON(w, http.StatusOK, user)
}

func handleUserLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	username := r.URL.Query().Get("username")
	password := r.URL.Query().Get("password")

	if username == "" || password == "" {
		writeError(w, http.StatusBadRequest, "Invalid credentials")
		return
	}

	var storedPassword string
	err := db.QueryRow(`SELECT password FROM users WHERE username=$1`, username).Scan(&storedPassword)
	if err == sql.ErrNoRows {
		writeError(w, http.StatusBadRequest, "Invalid credentials")
		return
	}
	if err != nil {
		log.Printf("Error logging in: %v", err)
		writeError(w, http.StatusInternalServerError, "Internal server error")
		return
	}

	if storedPassword != password {
		writeError(w, http.StatusBadRequest, "Invalid credentials")
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode("Logged in as " + username)
}

func handleUserByName(w http.ResponseWriter, r *http.Request) {
	username := strings.TrimPrefix(r.URL.Path, "/user/")
	if username == "" {
		writeError(w, http.StatusBadRequest, "Invalid username")
		return
	}

	switch r.Method {
	case http.MethodGet:
		getUserByName(w, username)
	case http.MethodPut:
		updateUser(w, r, username)
	case http.MethodDelete:
		deleteUser(w, username)
	default:
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
	}
}

func getUserByName(w http.ResponseWriter, username string) {
	var user User
	err := db.QueryRow(
		`SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username=$1`,
		username,
	).Scan(&user.ID, &user.Username, &user.FirstName, &user.LastName, &user.Email, &user.Password, &user.Phone, &user.UserStatus)
	if err == sql.ErrNoRows {
		writeError(w, http.StatusNotFound, "User not found")
		return
	}
	if err != nil {
		log.Printf("Error getting user: %v", err)
		writeError(w, http.StatusInternalServerError, "Internal server error")
		return
	}
	writeJSON(w, http.StatusOK, user)
}

func updateUser(w http.ResponseWriter, r *http.Request, username string) {
	var user User
	if err := json.NewDecoder(r.Body).Decode(&user); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	dbMu.Lock()
	defer dbMu.Unlock()

	var exists bool
	err := db.QueryRow(`SELECT EXISTS(SELECT 1 FROM users WHERE username=$1)`, username).Scan(&exists)
	if err != nil || !exists {
		writeError(w, http.StatusNotFound, "User not found")
		return
	}

	_, err = db.Exec(
		`UPDATE users SET username=$1, first_name=$2, last_name=$3, email=$4, password=$5, phone=$6, user_status=$7 WHERE username=$8`,
		user.Username, user.FirstName, user.LastName, user.Email, user.Password, user.Phone, user.UserStatus, username,
	)
	if err != nil {
		log.Printf("Error updating user: %v", err)
		writeError(w, http.StatusInternalServerError, "Internal server error")
		return
	}

	// Fetch updated user
	var updated User
	err = db.QueryRow(
		`SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username=$1`,
		user.Username,
	).Scan(&updated.ID, &updated.Username, &updated.FirstName, &updated.LastName, &updated.Email, &updated.Password, &updated.Phone, &updated.UserStatus)
	if err != nil {
		log.Printf("Error fetching updated user: %v", err)
		writeError(w, http.StatusInternalServerError, "Internal server error")
		return
	}

	writeJSON(w, http.StatusOK, updated)
}

func deleteUser(w http.ResponseWriter, username string) {
	dbMu.Lock()
	defer dbMu.Unlock()

	result, err := db.Exec(`DELETE FROM users WHERE username=$1`, username)
	if err != nil {
		log.Printf("Error deleting user: %v", err)
		writeError(w, http.StatusInternalServerError, "Internal server error")
		return
	}
	rowsAffected, _ := result.RowsAffected()
	if rowsAffected == 0 {
		writeError(w, http.StatusNotFound, "User not found")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"message": "successful operation"})
}

// PostgreSQL array helpers

type pqStringArray []string

func (a pqStringArray) Value() ([]byte, error) {
	if a == nil {
		return []byte("{}"), nil
	}
	result := "{"
	for i, s := range a {
		if i > 0 {
			result += ","
		}
		// Escape the string
		escaped := strings.ReplaceAll(s, `\`, `\\`)
		escaped = strings.ReplaceAll(escaped, `"`, `\"`)
		result += `"` + escaped + `"`
	}
	result += "}"
	return []byte(result), nil
}

func (a pqStringArray) String() string {
	b, _ := a.Value()
	return string(b)
}

type pqStringArrayScanner []string

func (a *pqStringArrayScanner) Scan(src interface{}) error {
	if src == nil {
		*a = []string{}
		return nil
	}

	var source string
	switch v := src.(type) {
	case []byte:
		source = string(v)
	case string:
		source = v
	default:
		return fmt.Errorf("cannot scan %T into pqStringArrayScanner", src)
	}

	// Parse PostgreSQL array format: {elem1,elem2,...}
	source = strings.TrimSpace(source)
	if source == "{}" || source == "" {
		*a = []string{}
		return nil
	}

	// Remove outer braces
	source = source[1 : len(source)-1]

	var result []string
	var current strings.Builder
	inQuote := false
	escaped := false

	for i := 0; i < len(source); i++ {
		c := source[i]
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
			inQuote = !inQuote
			continue
		}
		if c == ',' && !inQuote {
			result = append(result, current.String())
			current.Reset()
			continue
		}
		current.WriteByte(c)
	}
	result = append(result, current.String())

	*a = result
	return nil
}

func main() {
	initDB()

	mux := http.NewServeMux()

	// Pet routes
	mux.HandleFunc("/pet/findByStatus", handleFindPetsByStatus)
	mux.HandleFunc("/pet/", func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimPrefix(r.URL.Path, "/pet/")
		if path == "" {
			handlePet(w, r)
			return
		}
		// Check if it's findByStatus (shouldn't reach here due to explicit route)
		if strings.HasPrefix(path, "findByStatus") {
			handleFindPetsByStatus(w, r)
			return
		}
		handlePetByID(w, r)
	})
	mux.HandleFunc("/pet", handlePet)

	// Store routes
	mux.HandleFunc("/store/order/", func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimPrefix(r.URL.Path, "/store/order/")
		if path == "" {
			handleStoreOrder(w, r)
			return
		}
		handleOrderByID(w, r)
	})
	mux.HandleFunc("/store/order", handleStoreOrder)

	// User routes
	mux.HandleFunc("/user/login", handleUserLogin)
	mux.HandleFunc("/user/", func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimPrefix(r.URL.Path, "/user/")
		if path == "" {
			handleUser(w, r)
			return
		}
		if path == "login" {
			handleUserLogin(w, r)
			return
		}
		handleUserByName(w, r)
	})
	mux.HandleFunc("/user", handleUser)

	port := getEnv("PORT", "5001")
	addr := "0.0.0.0:" + port
	log.Printf("Server starting on %s", addr)
	log.Fatal(http.ListenAndServe(addr, mux))
}