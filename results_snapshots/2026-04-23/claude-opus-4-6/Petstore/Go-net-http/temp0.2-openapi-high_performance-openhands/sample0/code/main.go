package main

import (
	"database/sql"
	"database/sql/driver"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

var db *sql.DB

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

// Simple in-memory cache for pets by ID
type petCache struct {
	mu    sync.RWMutex
	items map[int64]*Pet
}

var pCache = &petCache{items: make(map[int64]*Pet)}

func (c *petCache) Get(id int64) (*Pet, bool) {
	c.mu.RLock()
	p, ok := c.items[id]
	c.mu.RUnlock()
	return p, ok
}

func (c *petCache) Set(p *Pet) {
	c.mu.Lock()
	c.items[p.ID] = p
	c.mu.Unlock()
}

func (c *petCache) Delete(id int64) {
	c.mu.Lock()
	delete(c.items, id)
	c.mu.Unlock()
}

func (c *petCache) Invalidate() {
	c.mu.Lock()
	c.items = make(map[int64]*Pet)
	c.mu.Unlock()
}

func initDB() {
	host := getEnv("DB_HOST", "localhost")
	port := getEnv("DB_PORT", "5432")
	user := getEnv("DB_USER", "postgres")
	password := getEnv("DB_PASSWORD", "postgres")
	dbname := getEnv("DB_NAME", "testdb")

	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, dbname)

	var err error
	db, err = sql.Open("postgres", connStr)
	if err != nil {
		log.Fatal("Failed to open database:", err)
	}

	// Connection pool settings for high performance
	db.SetMaxOpenConns(100)
	db.SetMaxIdleConns(50)
	db.SetConnMaxLifetime(5 * time.Minute)
	db.SetConnMaxIdleTime(1 * time.Minute)

	if err = db.Ping(); err != nil {
		log.Fatal("Failed to ping database:", err)
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
		`CREATE INDEX IF NOT EXISTS idx_pets_status ON pets(status)`,
		`CREATE TABLE IF NOT EXISTS orders (
			id BIGSERIAL PRIMARY KEY,
			pet_id BIGINT DEFAULT 0,
			quantity INT DEFAULT 0,
			ship_date TIMESTAMPTZ,
			status TEXT DEFAULT 'placed',
			complete BOOLEAN DEFAULT false
		)`,
		`CREATE TABLE IF NOT EXISTS users (
			id BIGSERIAL PRIMARY KEY,
			username TEXT UNIQUE NOT NULL,
			first_name TEXT DEFAULT '',
			last_name TEXT DEFAULT '',
			email TEXT DEFAULT '',
			password TEXT DEFAULT '',
			phone TEXT DEFAULT '',
			user_status INT DEFAULT 0
		)`,
		`CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)`,
	}

	for _, q := range queries {
		if _, err := db.Exec(q); err != nil {
			log.Fatal("Failed to create table:", err)
		}
	}
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
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

// Router
func main() {
	initDB()

	mux := http.NewServeMux()

	mux.HandleFunc("/pet/findByStatus", handleFindPetsByStatus)
	mux.HandleFunc("/pet/", handlePetWithID)
	mux.HandleFunc("/pet", handlePet)

	mux.HandleFunc("/store/order/", handleOrderWithID)
	mux.HandleFunc("/store/order", handleOrder)

	mux.HandleFunc("/user/login", handleUserLogin)
	mux.HandleFunc("/user/", handleUserWithUsername)
	mux.HandleFunc("/user", handleUser)

	port := getEnv("PORT", "5001")
	log.Printf("Starting server on 0.0.0.0:%s", port)
	log.Fatal(http.ListenAndServe("0.0.0.0:"+port, mux))
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

func handlePetWithID(w http.ResponseWriter, r *http.Request) {
	// Parse /pet/{petId}
	path := strings.TrimPrefix(r.URL.Path, "/pet/")
	if path == "" || strings.Contains(path, "/") {
		http.NotFound(w, r)
		return
	}

	petID, err := strconv.ParseInt(path, 10, 64)
	if err != nil {
		http.NotFound(w, r)
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

func handleFindPetsByStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	findPetsByStatus(w, r)
}

func addPet(w http.ResponseWriter, r *http.Request) {
	var pet Pet
	if err := json.NewDecoder(r.Body).Decode(&pet); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	if pet.Name == "" || pet.PhotoUrls == nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	if pet.Status == "" {
		pet.Status = "available"
	}

	err := db.QueryRow(
		`INSERT INTO pets (name, photo_urls, status) VALUES ($1, $2, $3) RETURNING id`,
		pet.Name, pqStringArray(pet.PhotoUrls), pet.Status,
	).Scan(&pet.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Failed to add pet")
		return
	}

	pCache.Set(&pet)
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

	result, err := db.Exec(
		`UPDATE pets SET name=$1, photo_urls=$2, status=$3 WHERE id=$4`,
		pet.Name, pqStringArray(pet.PhotoUrls), pet.Status, pet.ID,
	)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Failed to update pet")
		return
	}

	rows, _ := result.RowsAffected()
	if rows == 0 {
		writeError(w, http.StatusNotFound, "Pet not found")
		return
	}

	pCache.Set(&pet)
	writeJSON(w, http.StatusOK, pet)
}

func findPetsByStatus(w http.ResponseWriter, r *http.Request) {
	status := r.URL.Query().Get("status")
	if status == "" {
		writeJSON(w, http.StatusOK, []Pet{})
		return
	}

	rows, err := db.Query(
		`SELECT id, name, photo_urls, status FROM pets WHERE status=$1`, status,
	)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Failed to find pets")
		return
	}
	defer rows.Close()

	pets := make([]Pet, 0)
	for rows.Next() {
		var p Pet
		var urls pqStringArray
		if err := rows.Scan(&p.ID, &p.Name, &urls, &p.Status); err != nil {
			continue
		}
		p.PhotoUrls = []string(urls)
		pets = append(pets, p)
	}

	writeJSON(w, http.StatusOK, pets)
}

func getPetByID(w http.ResponseWriter, id int64) {
	// Check cache first
	if p, ok := pCache.Get(id); ok {
		writeJSON(w, http.StatusOK, *p)
		return
	}

	var p Pet
	var urls pqStringArray
	err := db.QueryRow(
		`SELECT id, name, photo_urls, status FROM pets WHERE id=$1`, id,
	).Scan(&p.ID, &p.Name, &urls, &p.Status)
	if err == sql.ErrNoRows {
		writeError(w, http.StatusNotFound, "Pet not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Failed to get pet")
		return
	}
	p.PhotoUrls = []string(urls)

	pCache.Set(&p)
	writeJSON(w, http.StatusOK, p)
}

func deletePet(w http.ResponseWriter, id int64) {
	result, err := db.Exec(`DELETE FROM pets WHERE id=$1`, id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Failed to delete pet")
		return
	}

	rows, _ := result.RowsAffected()
	if rows == 0 {
		writeError(w, http.StatusNotFound, "Pet not found")
		return
	}

	pCache.Delete(id)
	writeJSON(w, http.StatusOK, map[string]string{"message": "Pet deleted"})
}

// Order handlers
func handleOrder(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	placeOrder(w, r)
}

func handleOrderWithID(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/store/order/")
	if path == "" || strings.Contains(path, "/") {
		http.NotFound(w, r)
		return
	}

	orderID, err := strconv.ParseInt(path, 10, 64)
	if err != nil {
		http.NotFound(w, r)
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

func placeOrder(w http.ResponseWriter, r *http.Request) {
	var order Order
	if err := json.NewDecoder(r.Body).Decode(&order); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	if order.Status == "" {
		order.Status = "placed"
	}

	var shipDate *time.Time
	if order.ShipDate != "" {
		t, err := time.Parse(time.RFC3339, order.ShipDate)
		if err == nil {
			shipDate = &t
		}
	}

	err := db.QueryRow(
		`INSERT INTO orders (pet_id, quantity, ship_date, status, complete) VALUES ($1, $2, $3, $4, $5) RETURNING id`,
		order.PetID, order.Quantity, shipDate, order.Status, order.Complete,
	).Scan(&order.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Failed to place order")
		return
	}

	writeJSON(w, http.StatusOK, order)
}

func getOrderByID(w http.ResponseWriter, id int64) {
	var o Order
	var shipDate *time.Time
	err := db.QueryRow(
		`SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id=$1`, id,
	).Scan(&o.ID, &o.PetID, &o.Quantity, &shipDate, &o.Status, &o.Complete)
	if err == sql.ErrNoRows {
		writeError(w, http.StatusNotFound, "Order not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Failed to get order")
		return
	}
	if shipDate != nil {
		o.ShipDate = shipDate.Format(time.RFC3339)
	}

	writeJSON(w, http.StatusOK, o)
}

func deleteOrder(w http.ResponseWriter, id int64) {
	result, err := db.Exec(`DELETE FROM orders WHERE id=$1`, id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Failed to delete order")
		return
	}

	rows, _ := result.RowsAffected()
	if rows == 0 {
		writeError(w, http.StatusNotFound, "Order not found")
		return
	}

	writeJSON(w, http.StatusOK, map[string]string{"message": "Order deleted"})
}

// User handlers
func handleUser(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	createUser(w, r)
}

func handleUserLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	loginUser(w, r)
}

func handleUserWithUsername(w http.ResponseWriter, r *http.Request) {
	// Handle /user/login specially - but it's already handled by a more specific route
	path := strings.TrimPrefix(r.URL.Path, "/user/")
	if path == "" || strings.Contains(path, "/") {
		http.NotFound(w, r)
		return
	}

	// Don't handle /user/login here
	if path == "login" {
		handleUserLogin(w, r)
		return
	}

	username := path

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

func createUser(w http.ResponseWriter, r *http.Request) {
	var user User
	if err := json.NewDecoder(r.Body).Decode(&user); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	err := db.QueryRow(
		`INSERT INTO users (username, first_name, last_name, email, password, phone, user_status) 
		 VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id`,
		user.Username, user.FirstName, user.LastName, user.Email, user.Password, user.Phone, user.UserStatus,
	).Scan(&user.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Failed to create user")
		return
	}

	writeJSON(w, http.StatusOK, user)
}

func getUserByName(w http.ResponseWriter, username string) {
	var u User
	err := db.QueryRow(
		`SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username=$1`, username,
	).Scan(&u.ID, &u.Username, &u.FirstName, &u.LastName, &u.Email, &u.Password, &u.Phone, &u.UserStatus)
	if err == sql.ErrNoRows {
		writeError(w, http.StatusNotFound, "User not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Failed to get user")
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

	result, err := db.Exec(
		`UPDATE users SET username=$1, first_name=$2, last_name=$3, email=$4, password=$5, phone=$6, user_status=$7 WHERE username=$8`,
		user.Username, user.FirstName, user.LastName, user.Email, user.Password, user.Phone, user.UserStatus, username,
	)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Failed to update user")
		return
	}

	rows, _ := result.RowsAffected()
	if rows == 0 {
		writeError(w, http.StatusNotFound, "User not found")
		return
	}

	// Fetch the updated user to get the ID
	var u User
	err = db.QueryRow(
		`SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username=$1`,
		user.Username,
	).Scan(&u.ID, &u.Username, &u.FirstName, &u.LastName, &u.Email, &u.Password, &u.Phone, &u.UserStatus)
	if err != nil {
		writeJSON(w, http.StatusOK, user)
		return
	}

	writeJSON(w, http.StatusOK, u)
}

func deleteUser(w http.ResponseWriter, username string) {
	result, err := db.Exec(`DELETE FROM users WHERE username=$1`, username)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Failed to delete user")
		return
	}

	rows, _ := result.RowsAffected()
	if rows == 0 {
		writeError(w, http.StatusNotFound, "User not found")
		return
	}

	writeJSON(w, http.StatusOK, map[string]string{"message": "User deleted"})
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
	if err == sql.ErrNoRows {
		writeError(w, http.StatusBadRequest, "Invalid credentials")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Failed to login")
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

// pqStringArray is a helper type for PostgreSQL text arrays
type pqStringArray []string

func (a pqStringArray) Value() (driver.Value, error) {
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

func (a *pqStringArray) Scan(src interface{}) error {
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
		return fmt.Errorf("unsupported type for pqStringArray: %T", src)
	}

	// Parse PostgreSQL array format: {elem1,elem2,...}
	source = strings.TrimPrefix(source, "{")
	source = strings.TrimSuffix(source, "}")

	if source == "" {
		*a = []string{}
		return nil
	}

	*a = parsePostgresArray(source)
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
