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

func newPetCache() *petCache {
	return &petCache{items: make(map[int64]*Pet)}
}

func (c *petCache) Get(id int64) (*Pet, bool) {
	c.mu.RLock()
	p, ok := c.items[id]
	c.mu.RUnlock()
	if !ok {
		return nil, false
	}
	cp := *p
	cp.PhotoUrls = make([]string, len(p.PhotoUrls))
	copy(cp.PhotoUrls, p.PhotoUrls)
	return &cp, true
}

func (c *petCache) Set(p *Pet) {
	cp := *p
	cp.PhotoUrls = make([]string, len(p.PhotoUrls))
	copy(cp.PhotoUrls, p.PhotoUrls)
	c.mu.Lock()
	c.items[cp.ID] = &cp
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

var pCache *petCache

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

	// Connection pooling settings for high workload
	db.SetMaxOpenConns(100)
	db.SetMaxIdleConns(50)
	db.SetConnMaxLifetime(5 * time.Minute)
	db.SetConnMaxIdleTime(2 * time.Minute)

	for i := 0; i < 30; i++ {
		err = db.Ping()
		if err == nil {
			break
		}
		time.Sleep(time.Second)
	}
	if err != nil {
		log.Fatal("Failed to connect to database:", err)
	}

	createTables()
}

func createTables() {
	queries := []string{
		`CREATE TABLE IF NOT EXISTS pets (
			id BIGSERIAL PRIMARY KEY,
			name TEXT NOT NULL,
			photo_urls TEXT[] NOT NULL DEFAULT '{}',
			status TEXT NOT NULL DEFAULT 'available'
		)`,
		`CREATE INDEX IF NOT EXISTS idx_pets_status ON pets(status)`,

		`CREATE TABLE IF NOT EXISTS orders (
			id BIGSERIAL PRIMARY KEY,
			pet_id BIGINT NOT NULL DEFAULT 0,
			quantity INT NOT NULL DEFAULT 0,
			ship_date TIMESTAMPTZ,
			status TEXT NOT NULL DEFAULT 'placed',
			complete BOOLEAN NOT NULL DEFAULT false
		)`,

		`CREATE TABLE IF NOT EXISTS users (
			id BIGSERIAL PRIMARY KEY,
			username TEXT UNIQUE NOT NULL,
			first_name TEXT NOT NULL DEFAULT '',
			last_name TEXT NOT NULL DEFAULT '',
			email TEXT NOT NULL DEFAULT '',
			password TEXT NOT NULL DEFAULT '',
			phone TEXT NOT NULL DEFAULT '',
			user_status INT NOT NULL DEFAULT 0
		)`,
		`CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)`,
	}

	for _, q := range queries {
		if _, err := db.Exec(q); err != nil {
			log.Fatal("Failed to create table:", err)
		}
	}
}

func getEnv(key, defaultVal string) string {
	if val := os.Getenv(key); val != "" {
		return val
	}
	return defaultVal
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
func setupRoutes() http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("/pet/findByStatus", handleFindPetsByStatus)
	mux.HandleFunc("/pet/", handlePetWithID)
	mux.HandleFunc("/pet", handlePet)

	mux.HandleFunc("/store/order/", handleOrderWithID)
	mux.HandleFunc("/store/order", handlePlaceOrder)

	mux.HandleFunc("/user/login", handleUserLogin)
	mux.HandleFunc("/user/", handleUserWithUsername)
	mux.HandleFunc("/user", handleCreateUser)

	return mux
}

// Pet handlers
func handlePet(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/pet" {
		http.NotFound(w, r)
		return
	}
	switch r.Method {
	case http.MethodPost:
		addPet(w, r)
	case http.MethodPut:
		updatePet(w, r)
	default:
		w.WriteHeader(http.StatusMethodNotAllowed)
	}
}

func handlePetWithID(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Path
	if strings.HasPrefix(path, "/pet/findByStatus") {
		handleFindPetsByStatus(w, r)
		return
	}

	idStr := strings.TrimPrefix(path, "/pet/")
	if idStr == "" {
		http.NotFound(w, r)
		return
	}

	petID, err := strconv.ParseInt(idStr, 10, 64)
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
		w.WriteHeader(http.StatusMethodNotAllowed)
	}
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
		writeError(w, http.StatusInternalServerError, "Database error")
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

	if pet.Status == "" {
		pet.Status = "available"
	}

	res, err := db.Exec(
		`UPDATE pets SET name=$1, photo_urls=$2, status=$3 WHERE id=$4`,
		pet.Name, pqStringArray(pet.PhotoUrls), pet.Status, pet.ID,
	)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}

	rowsAffected, _ := res.RowsAffected()
	if rowsAffected == 0 {
		writeError(w, http.StatusNotFound, "Pet not found")
		return
	}

	pCache.Set(&pet)
	writeJSON(w, http.StatusOK, pet)
}

func handleFindPetsByStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}

	status := r.URL.Query().Get("status")
	if status == "" {
		writeJSON(w, http.StatusOK, []Pet{})
		return
	}

	rows, err := db.Query(`SELECT id, name, photo_urls, status FROM pets WHERE status=$1`, status)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}
	defer rows.Close()

	pets := make([]Pet, 0)
	for rows.Next() {
		var p Pet
		var urls pqStringArray
		if err := rows.Scan(&p.ID, &p.Name, &urls, &p.Status); err != nil {
			writeError(w, http.StatusInternalServerError, "Scan error")
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

func getPetByID(w http.ResponseWriter, id int64) {
	// Check cache first
	if p, ok := pCache.Get(id); ok {
		writeJSON(w, http.StatusOK, p)
		return
	}

	var p Pet
	var urls pqStringArray
	err := db.QueryRow(`SELECT id, name, photo_urls, status FROM pets WHERE id=$1`, id).
		Scan(&p.ID, &p.Name, &urls, &p.Status)
	if err == sql.ErrNoRows {
		writeError(w, http.StatusNotFound, "Pet not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}
	p.PhotoUrls = []string(urls)
	if p.PhotoUrls == nil {
		p.PhotoUrls = []string{}
	}

	pCache.Set(&p)
	writeJSON(w, http.StatusOK, p)
}

func deletePet(w http.ResponseWriter, id int64) {
	res, err := db.Exec(`DELETE FROM pets WHERE id=$1`, id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}

	rowsAffected, _ := res.RowsAffected()
	if rowsAffected == 0 {
		writeError(w, http.StatusNotFound, "Pet not found")
		return
	}

	pCache.Delete(id)
	w.WriteHeader(http.StatusOK)
}

// Order handlers
func handlePlaceOrder(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/store/order" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}

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
		if err != nil {
			t, err = time.Parse("2006-01-02T15:04:05.000Z", order.ShipDate)
			if err != nil {
				writeError(w, http.StatusBadRequest, "Invalid ship date")
				return
			}
		}
		shipDate = &t
	}

	err := db.QueryRow(
		`INSERT INTO orders (pet_id, quantity, ship_date, status, complete) VALUES ($1, $2, $3, $4, $5) RETURNING id`,
		order.PetID, order.Quantity, shipDate, order.Status, order.Complete,
	).Scan(&order.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}

	writeJSON(w, http.StatusOK, order)
}

func handleOrderWithID(w http.ResponseWriter, r *http.Request) {
	idStr := strings.TrimPrefix(r.URL.Path, "/store/order/")
	if idStr == "" {
		http.NotFound(w, r)
		return
	}

	orderID, err := strconv.ParseInt(idStr, 10, 64)
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
		w.WriteHeader(http.StatusMethodNotAllowed)
	}
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
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}
	if shipDate != nil {
		o.ShipDate = shipDate.Format(time.RFC3339)
	}
	writeJSON(w, http.StatusOK, o)
}

func deleteOrder(w http.ResponseWriter, id int64) {
	res, err := db.Exec(`DELETE FROM orders WHERE id=$1`, id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}
	rowsAffected, _ := res.RowsAffected()
	if rowsAffected == 0 {
		writeError(w, http.StatusNotFound, "Order not found")
		return
	}
	w.WriteHeader(http.StatusOK)
}

// User handlers
func handleCreateUser(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/user" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}

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
		writeError(w, http.StatusBadRequest, "User creation failed")
		return
	}

	writeJSON(w, http.StatusOK, user)
}

func handleUserLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
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
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}

	if storedPassword != password {
		writeError(w, http.StatusBadRequest, "Invalid credentials")
		return
	}

	writeJSON(w, http.StatusOK, "Logged in successfully")
}

func handleUserWithUsername(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Path
	if strings.HasPrefix(path, "/user/login") {
		handleUserLogin(w, r)
		return
	}

	username := strings.TrimPrefix(path, "/user/")
	if username == "" {
		http.NotFound(w, r)
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
		w.WriteHeader(http.StatusMethodNotAllowed)
	}
}

func getUserByName(w http.ResponseWriter, username string) {
	var u User
	err := db.QueryRow(
		`SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username=$1`,
		username,
	).Scan(&u.ID, &u.Username, &u.FirstName, &u.LastName, &u.Email, &u.Password, &u.Phone, &u.UserStatus)
	if err == sql.ErrNoRows {
		writeError(w, http.StatusNotFound, "User not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
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

	res, err := db.Exec(
		`UPDATE users SET username=$1, first_name=$2, last_name=$3, email=$4, password=$5, phone=$6, user_status=$7 WHERE username=$8`,
		user.Username, user.FirstName, user.LastName, user.Email, user.Password, user.Phone, user.UserStatus, username,
	)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}
	rowsAffected, _ := res.RowsAffected()
	if rowsAffected == 0 {
		writeError(w, http.StatusNotFound, "User not found")
		return
	}

	// Return the updated user with its ID
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
	res, err := db.Exec(`DELETE FROM users WHERE username=$1`, username)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}
	rowsAffected, _ := res.RowsAffected()
	if rowsAffected == 0 {
		writeError(w, http.StatusNotFound, "User not found")
		return
	}
	w.WriteHeader(http.StatusOK)
}

// pqStringArray implements the driver.Valuer and sql.Scanner interfaces for []string
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

func (a *pqStringArray) Scan(src interface{}) error {
	if src == nil {
		*a = []string{}
		return nil
	}

	var source string
	switch v := src.(type) {
	case string:
		source = v
	case []byte:
		source = string(v)
	default:
		return fmt.Errorf("unsupported type for pqStringArray: %T", src)
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
	pCache = newPetCache()
	initDB()
	defer db.Close()

	handler := setupRoutes()

	port := getEnv("PORT", "5001")
	server := &http.Server{
		Addr:         "0.0.0.0:" + port,
		Handler:      handler,
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 10 * time.Second,
		IdleTimeout:  120 * time.Second,
	}

	log.Printf("Server starting on port %s", port)
	if err := server.ListenAndServe(); err != nil {
		log.Fatal(err)
	}
}
