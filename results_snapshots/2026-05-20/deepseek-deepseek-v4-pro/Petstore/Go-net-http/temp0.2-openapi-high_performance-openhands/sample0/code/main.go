package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/lib/pq"
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

var db *sql.DB

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func initDB() {
	host := os.Getenv("DB_HOST")
	port := os.Getenv("DB_PORT")
	user := os.Getenv("DB_USER")
	password := os.Getenv("DB_PASSWORD")
	dbname := os.Getenv("DB_NAME")

	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, dbname)

	var err error
	db, err = sql.Open("postgres", connStr)
	if err != nil {
		log.Fatalf("Failed to open database: %v", err)
	}

	db.SetMaxOpenConns(100)
	db.SetMaxIdleConns(25)
	db.SetConnMaxLifetime(5 * time.Minute)

	if err = db.Ping(); err != nil {
		log.Fatalf("Failed to ping database: %v", err)
	}

	schema := `
	CREATE TABLE IF NOT EXISTS pets (
		id BIGSERIAL PRIMARY KEY,
		name TEXT NOT NULL,
		photo_urls TEXT[] NOT NULL DEFAULT '{}',
		status TEXT NOT NULL DEFAULT 'available'
	);

	CREATE TABLE IF NOT EXISTS orders (
		id BIGSERIAL PRIMARY KEY,
		pet_id BIGINT NOT NULL DEFAULT 0,
		quantity INT NOT NULL DEFAULT 0,
		ship_date TEXT NOT NULL DEFAULT '',
		status TEXT NOT NULL DEFAULT 'placed',
		complete BOOLEAN NOT NULL DEFAULT false
	);

	CREATE TABLE IF NOT EXISTS users (
		id BIGSERIAL PRIMARY KEY,
		username TEXT NOT NULL UNIQUE,
		first_name TEXT NOT NULL DEFAULT '',
		last_name TEXT NOT NULL DEFAULT '',
		email TEXT NOT NULL DEFAULT '',
		password TEXT NOT NULL DEFAULT '',
		phone TEXT NOT NULL DEFAULT '',
		user_status INT NOT NULL DEFAULT 0
	);

	CREATE INDEX IF NOT EXISTS idx_pets_status ON pets(status);
	CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
	`

	if _, err = db.Exec(schema); err != nil {
		log.Fatalf("Failed to create tables: %v", err)
	}

	log.Println("Database initialized successfully")
}

// ---- Pet Handlers ----

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
		"INSERT INTO pets (name, photo_urls, status) VALUES ($1, $2, $3) RETURNING id",
		pet.Name, pq.Array(pet.PhotoUrls), pet.Status,
	).Scan(&pet.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Failed to create pet")
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
	if pet.Status == "" {
		pet.Status = "available"
	}

	result, err := db.Exec(
		"UPDATE pets SET name=$1, photo_urls=$2, status=$3 WHERE id=$4",
		pet.Name, pq.Array(pet.PhotoUrls), pet.Status, pet.ID,
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

	writeJSON(w, http.StatusOK, pet)
}

func findPetsByStatus(w http.ResponseWriter, r *http.Request) {
	status := r.URL.Query().Get("status")
	validStatuses := map[string]bool{"available": true, "pending": true, "sold": true}
	if !validStatuses[status] {
		writeError(w, http.StatusBadRequest, "Invalid status value")
		return
	}

	rows, err := db.Query("SELECT id, name, photo_urls, status FROM pets WHERE status=$1", status)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Query failed")
		return
	}
	defer rows.Close()

	pets := make([]Pet, 0)
	for rows.Next() {
		var p Pet
		if err := rows.Scan(&p.ID, &p.Name, pq.Array(&p.PhotoUrls), &p.Status); err != nil {
			continue
		}
		pets = append(pets, p)
	}

	writeJSON(w, http.StatusOK, pets)
}

func getPetById(w http.ResponseWriter, r *http.Request) {
	idStr := r.PathValue("petId")
	id, err := strconv.ParseInt(idStr, 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid pet ID")
		return
	}

	var pet Pet
	err = db.QueryRow("SELECT id, name, photo_urls, status FROM pets WHERE id=$1", id).
		Scan(&pet.ID, &pet.Name, pq.Array(&pet.PhotoUrls), &pet.Status)
	if err == sql.ErrNoRows {
		writeError(w, http.StatusNotFound, "Pet not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Query failed")
		return
	}

	writeJSON(w, http.StatusOK, pet)
}

func deletePet(w http.ResponseWriter, r *http.Request) {
	idStr := r.PathValue("petId")
	id, err := strconv.ParseInt(idStr, 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid pet ID")
		return
	}

	result, err := db.Exec("DELETE FROM pets WHERE id=$1", id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Delete failed")
		return
	}
	rows, _ := result.RowsAffected()
	if rows == 0 {
		writeError(w, http.StatusNotFound, "Pet not found")
		return
	}

	w.WriteHeader(http.StatusOK)
}

// ---- Store Handlers ----

func placeOrder(w http.ResponseWriter, r *http.Request) {
	var order Order
	if err := json.NewDecoder(r.Body).Decode(&order); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}
	if order.Status == "" {
		order.Status = "placed"
	}

	err := db.QueryRow(
		"INSERT INTO orders (pet_id, quantity, ship_date, status, complete) VALUES ($1, $2, $3, $4, $5) RETURNING id",
		order.PetID, order.Quantity, order.ShipDate, order.Status, order.Complete,
	).Scan(&order.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Failed to create order")
		return
	}

	writeJSON(w, http.StatusOK, order)
}

func getOrderById(w http.ResponseWriter, r *http.Request) {
	idStr := r.PathValue("orderId")
	id, err := strconv.ParseInt(idStr, 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid order ID")
		return
	}

	var order Order
	err = db.QueryRow("SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id=$1", id).
		Scan(&order.ID, &order.PetID, &order.Quantity, &order.ShipDate, &order.Status, &order.Complete)
	if err == sql.ErrNoRows {
		writeError(w, http.StatusNotFound, "Order not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Query failed")
		return
	}

	writeJSON(w, http.StatusOK, order)
}

func deleteOrder(w http.ResponseWriter, r *http.Request) {
	idStr := r.PathValue("orderId")
	id, err := strconv.ParseInt(idStr, 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid order ID")
		return
	}

	result, err := db.Exec("DELETE FROM orders WHERE id=$1", id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Delete failed")
		return
	}
	rows, _ := result.RowsAffected()
	if rows == 0 {
		writeError(w, http.StatusNotFound, "Order not found")
		return
	}

	w.WriteHeader(http.StatusOK)
}

// ---- User Handlers ----

func createUser(w http.ResponseWriter, r *http.Request) {
	var user User
	if err := json.NewDecoder(r.Body).Decode(&user); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	err := db.QueryRow(
		"INSERT INTO users (username, first_name, last_name, email, password, phone, user_status) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
		user.Username, user.FirstName, user.LastName, user.Email, user.Password, user.Phone, user.UserStatus,
	).Scan(&user.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Failed to create user")
		return
	}

	writeJSON(w, http.StatusOK, user)
}

func getUserByName(w http.ResponseWriter, r *http.Request) {
	username := r.PathValue("username")

	var user User
	err := db.QueryRow("SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username=$1", username).
		Scan(&user.ID, &user.Username, &user.FirstName, &user.LastName, &user.Email, &user.Password, &user.Phone, &user.UserStatus)
	if err == sql.ErrNoRows {
		writeError(w, http.StatusNotFound, "User not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Query failed")
		return
	}

	writeJSON(w, http.StatusOK, user)
}

func updateUser(w http.ResponseWriter, r *http.Request) {
	username := r.PathValue("username")

	var user User
	if err := json.NewDecoder(r.Body).Decode(&user); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	result, err := db.Exec(
		"UPDATE users SET username=$1, first_name=$2, last_name=$3, email=$4, password=$5, phone=$6, user_status=$7 WHERE username=$8",
		user.Username, user.FirstName, user.LastName, user.Email, user.Password, user.Phone, user.UserStatus, username,
	)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Update failed")
		return
	}
	rows, _ := result.RowsAffected()
	if rows == 0 {
		writeError(w, http.StatusNotFound, "User not found")
		return
	}

	writeJSON(w, http.StatusOK, user)
}

func deleteUser(w http.ResponseWriter, r *http.Request) {
	username := r.PathValue("username")

	result, err := db.Exec("DELETE FROM users WHERE username=$1", username)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Delete failed")
		return
	}
	rows, _ := result.RowsAffected()
	if rows == 0 {
		writeError(w, http.StatusNotFound, "User not found")
		return
	}

	w.WriteHeader(http.StatusOK)
}

func loginUser(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	w.Write([]byte(`"logged in"`))
}

func main() {
	initDB()
	defer db.Close()

	mux := http.NewServeMux()

	// Pet endpoints
	mux.HandleFunc("POST /pet", addPet)
	mux.HandleFunc("PUT /pet", updatePet)
	mux.HandleFunc("GET /pet/findByStatus", findPetsByStatus)
	mux.HandleFunc("GET /pet/{petId}", getPetById)
	mux.HandleFunc("DELETE /pet/{petId}", deletePet)

	// Store endpoints
	mux.HandleFunc("POST /store/order", placeOrder)
	mux.HandleFunc("GET /store/order/{orderId}", getOrderById)
	mux.HandleFunc("DELETE /store/order/{orderId}", deleteOrder)

	// User endpoints
	mux.HandleFunc("POST /user", createUser)
	mux.HandleFunc("GET /user/login", loginUser)
	mux.HandleFunc("GET /user/{username}", getUserByName)
	mux.HandleFunc("PUT /user/{username}", updateUser)
	mux.HandleFunc("DELETE /user/{username}", deleteUser)

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	addr := "0.0.0.0:" + port
	log.Printf("Server starting on %s", addr)
	log.Fatal(http.ListenAndServe(addr, mux))
}
