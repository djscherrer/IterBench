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

// Simple cache for pets by ID
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
	c.items[p.ID] = &cp
	c.mu.Unlock()
}

func (c *petCache) Delete(id int64) {
	c.mu.Lock()
	delete(c.items, id)
	c.mu.Unlock()
}

var pCache *petCache

func initDB() {
	host := envOrDefault("DB_HOST", "localhost")
	port := envOrDefault("DB_PORT", "5432")
	user := envOrDefault("DB_USER", "postgres")
	password := envOrDefault("DB_PASSWORD", "postgres")
	dbname := envOrDefault("DB_NAME", "testdb")

	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, dbname)

	var err error
	db, err = sql.Open("postgres", connStr)
	if err != nil {
		log.Fatalf("Failed to open database: %v", err)
	}

	db.SetMaxOpenConns(100)
	db.SetMaxIdleConns(50)
	db.SetConnMaxLifetime(5 * time.Minute)

	for i := 0; i < 30; i++ {
		err = db.Ping()
		if err == nil {
			break
		}
		time.Sleep(time.Second)
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
			log.Fatalf("Failed to create table: %v\nQuery: %s", err, q)
		}
	}
}

func envOrDefault(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
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
	if pet.Name == "" || pet.PhotoUrls == nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}
	if pet.Status == "" {
		pet.Status = "available"
	}

	photoUrls := "{" + joinQuoted(pet.PhotoUrls) + "}"
	err := db.QueryRow(
		`INSERT INTO pets (name, photo_urls, status) VALUES ($1, $2::text[], $3) RETURNING id`,
		pet.Name, photoUrls, pet.Status,
	).Scan(&pet.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
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

	photoUrls := "{" + joinQuoted(pet.PhotoUrls) + "}"
	res, err := db.Exec(
		`UPDATE pets SET name=$1, photo_urls=$2::text[], status=$3 WHERE id=$4`,
		pet.Name, photoUrls, pet.Status, pet.ID,
	)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		writeError(w, http.StatusNotFound, "Pet not found")
		return
	}
	pCache.Set(&pet)
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
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer rows.Close()

	pets := make([]Pet, 0)
	for rows.Next() {
		var p Pet
		var urls []byte
		if err := rows.Scan(&p.ID, &p.Name, &urls, &p.Status); err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		p.PhotoUrls = parsePostgresArray(string(urls))
		pets = append(pets, p)
	}
	writeJSON(w, http.StatusOK, pets)
}

func handlePetByID(w http.ResponseWriter, r *http.Request) {
	idStr := strings.TrimPrefix(r.URL.Path, "/pet/")
	id, err := strconv.ParseInt(idStr, 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid pet ID")
		return
	}

	switch r.Method {
	case http.MethodGet:
		getPetByID(w, id)
	case http.MethodDelete:
		deletePet(w, id)
	default:
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
	}
}

func getPetByID(w http.ResponseWriter, id int64) {
	if p, ok := pCache.Get(id); ok {
		writeJSON(w, http.StatusOK, p)
		return
	}

	var p Pet
	var urls []byte
	err := db.QueryRow(`SELECT id, name, photo_urls, status FROM pets WHERE id=$1`, id).
		Scan(&p.ID, &p.Name, &urls, &p.Status)
	if err == sql.ErrNoRows {
		writeError(w, http.StatusNotFound, "Pet not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	p.PhotoUrls = parsePostgresArray(string(urls))
	pCache.Set(&p)
	writeJSON(w, http.StatusOK, p)
}

func deletePet(w http.ResponseWriter, id int64) {
	res, err := db.Exec(`DELETE FROM pets WHERE id=$1`, id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		writeError(w, http.StatusNotFound, "Pet not found")
		return
	}
	pCache.Delete(id)
	writeJSON(w, http.StatusOK, map[string]string{"message": "successful operation"})
}

// Order handlers
func handleOrder(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var order Order
	if err := json.NewDecoder(r.Body).Decode(&order); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
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
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, order)
}

func handleOrderByID(w http.ResponseWriter, r *http.Request) {
	idStr := strings.TrimPrefix(r.URL.Path, "/store/order/")
	id, err := strconv.ParseInt(idStr, 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid order ID")
		return
	}

	switch r.Method {
	case http.MethodGet:
		getOrderByID(w, id)
	case http.MethodDelete:
		deleteOrder(w, id)
	default:
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
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
		writeError(w, http.StatusInternalServerError, err.Error())
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
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	n, _ := res.RowsAffected()
	if n == 0 {
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
		writeError(w, http.StatusInternalServerError, err.Error())
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
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}

	if storedPassword != password {
		writeError(w, http.StatusBadRequest, "Invalid credentials")
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode("Logged in user session: " + username)
}

func handleUserByUsername(w http.ResponseWriter, r *http.Request) {
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
		writeError(w, http.StatusInternalServerError, err.Error())
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
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		writeError(w, http.StatusNotFound, "User not found")
		return
	}

	// Fetch updated user to return with ID
	var u User
	err = db.QueryRow(
		`SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username=$1`,
		user.Username,
	).Scan(&u.ID, &u.Username, &u.FirstName, &u.LastName, &u.Email, &u.Password, &u.Phone, &u.UserStatus)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, u)
}

func deleteUser(w http.ResponseWriter, username string) {
	res, err := db.Exec(`DELETE FROM users WHERE username=$1`, username)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		writeError(w, http.StatusNotFound, "User not found")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"message": "successful operation"})
}

// Utility functions
func joinQuoted(ss []string) string {
	quoted := make([]string, len(ss))
	for i, s := range ss {
		escaped := strings.ReplaceAll(s, `\`, `\\`)
		escaped = strings.ReplaceAll(escaped, `"`, `\"`)
		quoted[i] = `"` + escaped + `"`
	}
	return strings.Join(quoted, ",")
}

func parsePostgresArray(s string) []string {
	s = strings.TrimSpace(s)
	if s == "{}" || s == "" {
		return []string{}
	}
	s = strings.TrimPrefix(s, "{")
	s = strings.TrimSuffix(s, "}")

	var result []string
	var current strings.Builder
	inQuotes := false
	escaped := false

	for _, ch := range s {
		if escaped {
			current.WriteRune(ch)
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
			result = append(result, current.String())
			current.Reset()
			continue
		}
		current.WriteRune(ch)
	}
	if current.Len() > 0 {
		result = append(result, current.String())
	}
	return result
}

// Router
func setupRouter() http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("/pet/findByStatus", handleFindPetsByStatus)
	mux.HandleFunc("/pet/", func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		if path == "/pet/" || path == "/pet" {
			handlePet(w, r)
			return
		}
		handlePetByID(w, r)
	})
	mux.HandleFunc("/pet", handlePet)

	mux.HandleFunc("/store/order/", func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		if path == "/store/order/" || path == "/store/order" {
			handleOrder(w, r)
			return
		}
		handleOrderByID(w, r)
	})
	mux.HandleFunc("/store/order", handleOrder)

	mux.HandleFunc("/user/login", handleUserLogin)
	mux.HandleFunc("/user/", func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		if path == "/user/" || path == "/user" {
			handleUser(w, r)
			return
		}
		// Make sure we don't match /user/login here
		trimmed := strings.TrimPrefix(path, "/user/")
		if trimmed == "login" {
			handleUserLogin(w, r)
			return
		}
		handleUserByUsername(w, r)
	})
	mux.HandleFunc("/user", handleUser)

	return mux
}

func main() {
	pCache = newPetCache()
	initDB()

	port := envOrDefault("PORT", "5001")
	server := &http.Server{
		Addr:         "0.0.0.0:" + port,
		Handler:      setupRouter(),
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 10 * time.Second,
		IdleTimeout:  120 * time.Second,
	}

	log.Printf("Server starting on port %s", port)
	if err := server.ListenAndServe(); err != nil {
		log.Fatalf("Server failed: %v", err)
	}
}
