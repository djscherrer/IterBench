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

	_ "github.com/lib/pq"
)

var db *sql.DB

type Pet struct {
	ID        int64    `json:"id"`
	Name      string   `json:"name"`
	PhotoUrls []string `json:"photoUrls"`
	Status    string   `json:"status,omitempty"`
}

type Order struct {
	ID       int64     `json:"id"`
	PetID    int64     `json:"petId"`
	Quantity int       `json:"quantity,omitempty"`
	ShipDate time.Time `json:"shipDate,omitempty"`
	Status   string    `json:"status,omitempty"`
	Complete bool      `json:"complete,omitempty"`
}

type User struct {
	ID         int64  `json:"id"`
	Username   string `json:"username"`
	FirstName  string `json:"firstName,omitempty"`
	LastName   string `json:"lastName,omitempty"`
	Email      string `json:"email,omitempty"`
	Password   string `json:"password,omitempty"`
	Phone      string `json:"phone,omitempty"`
	UserStatus int    `json:"userStatus,omitempty"`
}

func initDB() {
	var err error
	host := os.Getenv("DB_HOST")
	port := os.Getenv("DB_PORT")
	user := os.Getenv("DB_USER")
	password := os.Getenv("DB_PASSWORD")
	dbname := os.Getenv("DB_NAME")

	if host == "" {
		host = "localhost"
	}
	if port == "" {
		port = "5432"
	}
	if user == "" {
		user = "postgres"
	}
	if dbname == "" {
		dbname = "petstore"
	}

	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, dbname)

	db, err = sql.Open("postgres", connStr)
	if err != nil {
		log.Fatal(err)
	}

	err = db.Ping()
	if err != nil {
		log.Fatal(err)
	}

	createTables := `
	CREATE TABLE IF NOT EXISTS pets (
		id SERIAL PRIMARY KEY,
		name TEXT NOT NULL,
		photo_urls TEXT[],
		status TEXT CHECK (status IN ('available', 'pending', 'sold'))
	);
	
	CREATE TABLE IF NOT EXISTS orders (
		id SERIAL PRIMARY KEY,
		pet_id BIGINT NOT NULL,
		quantity INTEGER,
		ship_date TIMESTAMP,
		status TEXT CHECK (status IN ('placed', 'approved', 'delivered')),
		complete BOOLEAN DEFAULT false
	);
	
	CREATE TABLE IF NOT EXISTS users (
		id SERIAL PRIMARY KEY,
		username TEXT UNIQUE NOT NULL,
		first_name TEXT,
		last_name TEXT,
		email TEXT,
		password TEXT,
		phone TEXT,
		user_status INTEGER DEFAULT 0
	);
	`

	_, err = db.Exec(createTables)
	if err != nil {
		log.Fatal(err)
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func addPet(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var pet Pet
	if err := json.NewDecoder(r.Body).Decode(&pet); err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	if pet.Name == "" || len(pet.PhotoUrls) == 0 {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	var id int64
	err := db.QueryRow(`
		INSERT INTO pets (name, photo_urls, status)
		VALUES ($1, $2, $3)
		RETURNING id
	`, pet.Name, pqArray(pet.PhotoUrls), pet.Status).Scan(&id)

	if err != nil {
		http.Error(w, "Database error", http.StatusInternalServerError)
		return
	}

	pet.ID = id
	writeJSON(w, http.StatusOK, pet)
}

func updatePet(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPut {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var pet Pet
	if err := json.NewDecoder(r.Body).Decode(&pet); err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	if pet.Name == "" || len(pet.PhotoUrls) == 0 {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	result, err := db.Exec(`
		UPDATE pets
		SET name = $1, photo_urls = $2, status = $3
		WHERE id = $4
	`, pet.Name, pqArray(pet.PhotoUrls), pet.Status, pet.ID)

	if err != nil {
		http.Error(w, "Database error", http.StatusInternalServerError)
		return
	}

	rows, _ := result.RowsAffected()
	if rows == 0 {
		http.Error(w, "Pet not found", http.StatusNotFound)
		return
	}

	writeJSON(w, http.StatusOK, pet)
}

func findPetsByStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	status := r.URL.Query().Get("status")
	if status == "" {
		http.Error(w, "Missing status parameter", http.StatusBadRequest)
		return
	}

	validStatus := map[string]bool{"available": true, "pending": true, "sold": true}
	if !validStatus[status] {
		http.Error(w, "Invalid status value", http.StatusBadRequest)
		return
	}

	rows, err := db.Query(`
		SELECT id, name, photo_urls, status
		FROM pets
		WHERE status = $1
	`, status)
	if err != nil {
		http.Error(w, "Database error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	var pets []Pet
	for rows.Next() {
		var pet Pet
		var urls []string
		err := rows.Scan(&pet.ID, &pet.Name, &urls, &pet.Status)
		if err != nil {
			http.Error(w, "Database error", http.StatusInternalServerError)
			return
		}
		pet.PhotoUrls = urls
		pets = append(pets, pet)
	}

	writeJSON(w, http.StatusOK, pets)
}

func getPetById(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	pathParts := strings.Split(r.URL.Path, "/")
	if len(pathParts) < 3 {
		http.Error(w, "Invalid path", http.StatusBadRequest)
		return
	}
	petIdStr := pathParts[len(pathParts)-1]
	petId, err := strconv.ParseInt(petIdStr, 10, 64)
	if err != nil {
		http.Error(w, "Invalid pet ID", http.StatusBadRequest)
		return
	}

	var pet Pet
	var urls []string
	err = db.QueryRow(`
		SELECT id, name, photo_urls, status
		FROM pets
		WHERE id = $1
	`, petId).Scan(&pet.ID, &pet.Name, &urls, &pet.Status)

	if err == sql.ErrNoRows {
		http.Error(w, "Pet not found", http.StatusNotFound)
		return
	}
	if err != nil {
		http.Error(w, "Database error", http.StatusInternalServerError)
		return
	}

	pet.PhotoUrls = urls
	writeJSON(w, http.StatusOK, pet)
}

func deletePet(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodDelete {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	pathParts := strings.Split(r.URL.Path, "/")
	if len(pathParts) < 3 {
		http.Error(w, "Invalid path", http.StatusBadRequest)
		return
	}
	petIdStr := pathParts[len(pathParts)-1]
	petId, err := strconv.ParseInt(petIdStr, 10, 64)
	if err != nil {
		http.Error(w, "Invalid pet ID", http.StatusBadRequest)
		return
	}

	result, err := db.Exec(`DELETE FROM pets WHERE id = $1`, petId)
	if err != nil {
		http.Error(w, "Database error", http.StatusInternalServerError)
		return
	}

	rows, _ := result.RowsAffected()
	if rows == 0 {
		http.Error(w, "Pet not found", http.StatusNotFound)
		return
	}

	w.WriteHeader(http.StatusOK)
}

func placeOrder(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var order Order
	if err := json.NewDecoder(r.Body).Decode(&order); err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	var id int64
	err := db.QueryRow(`
		INSERT INTO orders (pet_id, quantity, ship_date, status, complete)
		VALUES ($1, $2, $3, $4, $5)
		RETURNING id
	`, order.PetID, order.Quantity, order.ShipDate, order.Status, order.Complete).Scan(&id)

	if err != nil {
		http.Error(w, "Database error", http.StatusInternalServerError)
		return
	}

	order.ID = id
	writeJSON(w, http.StatusOK, order)
}

func getOrderById(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	pathParts := strings.Split(r.URL.Path, "/")
	if len(pathParts) < 4 {
		http.Error(w, "Invalid path", http.StatusBadRequest)
		return
	}
	orderIdStr := pathParts[len(pathParts)-1]
	orderId, err := strconv.ParseInt(orderIdStr, 10, 64)
	if err != nil {
		http.Error(w, "Invalid order ID", http.StatusBadRequest)
		return
	}

	var order Order
	err = db.QueryRow(`
		SELECT id, pet_id, quantity, ship_date, status, complete
		FROM orders
		WHERE id = $1
	`, orderId).Scan(&order.ID, &order.PetID, &order.Quantity, &order.ShipDate, &order.Status, &order.Complete)

	if err == sql.ErrNoRows {
		http.Error(w, "Order not found", http.StatusNotFound)
		return
	}
	if err != nil {
		http.Error(w, "Database error", http.StatusInternalServerError)
		return
	}

	writeJSON(w, http.StatusOK, order)
}

func deleteOrder(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodDelete {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	pathParts := strings.Split(r.URL.Path, "/")
	if len(pathParts) < 4 {
		http.Error(w, "Invalid path", http.StatusBadRequest)
		return
	}
	orderIdStr := pathParts[len(pathParts)-1]
	orderId, err := strconv.ParseInt(orderIdStr, 10, 64)
	if err != nil {
		http.Error(w, "Invalid order ID", http.StatusBadRequest)
		return
	}

	result, err := db.Exec(`DELETE FROM orders WHERE id = $1`, orderId)
	if err != nil {
		http.Error(w, "Database error", http.StatusInternalServerError)
		return
	}

	rows, _ := result.RowsAffected()
	if rows == 0 {
		http.Error(w, "Order not found", http.StatusNotFound)
		return
	}

	w.WriteHeader(http.StatusOK)
}

func createUser(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var user User
	if err := json.NewDecoder(r.Body).Decode(&user); err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	if user.Username == "" {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	var id int64
	err := db.QueryRow(`
		INSERT INTO users (username, first_name, last_name, email, password, phone, user_status)
		VALUES ($1, $2, $3, $4, $5, $6, $7)
		RETURNING id
	`, user.Username, user.FirstName, user.LastName, user.Email, user.Password, user.Phone, user.UserStatus).Scan(&id)

	if err != nil {
		http.Error(w, "Database error", http.StatusInternalServerError)
		return
	}

	user.ID = id
	writeJSON(w, http.StatusOK, user)
}

func getUserByName(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	pathParts := strings.Split(r.URL.Path, "/")
	if len(pathParts) < 3 {
		http.Error(w, "Invalid path", http.StatusBadRequest)
		return
	}
	username := pathParts[len(pathParts)-1]

	var user User
	err := db.QueryRow(`
		SELECT id, username, first_name, last_name, email, password, phone, user_status
		FROM users
		WHERE username = $1
	`, username).Scan(&user.ID, &user.Username, &user.FirstName, &user.LastName, &user.Email, &user.Password, &user.Phone, &user.UserStatus)

	if err == sql.ErrNoRows {
		http.Error(w, "User not found", http.StatusNotFound)
		return
	}
	if err != nil {
		http.Error(w, "Database error", http.StatusInternalServerError)
		return
	}

	writeJSON(w, http.StatusOK, user)
}

func updateUser(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPut {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	pathParts := strings.Split(r.URL.Path, "/")
	if len(pathParts) < 3 {
		http.Error(w, "Invalid path", http.StatusBadRequest)
		return
	}
	username := pathParts[len(pathParts)-1]

	var user User
	if err := json.NewDecoder(r.Body).Decode(&user); err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	if user.Username == "" {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	result, err := db.Exec(`
		UPDATE users
		SET username = $1, first_name = $2, last_name = $3, email = $4, password = $5, phone = $6, user_status = $7
		WHERE username = $8
	`, user.Username, user.FirstName, user.LastName, user.Email, user.Password, user.Phone, user.UserStatus, username)

	if err != nil {
		http.Error(w, "Database error", http.StatusInternalServerError)
		return
	}

	rows, _ := result.RowsAffected()
	if rows == 0 {
		http.Error(w, "User not found", http.StatusNotFound)
		return
	}

	writeJSON(w, http.StatusOK, user)
}

func deleteUser(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodDelete {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	pathParts := strings.Split(r.URL.Path, "/")
	if len(pathParts) < 3 {
		http.Error(w, "Invalid path", http.StatusBadRequest)
		return
	}
	username := pathParts[len(pathParts)-1]

	result, err := db.Exec(`DELETE FROM users WHERE username = $1`, username)
	if err != nil {
		http.Error(w, "Database error", http.StatusInternalServerError)
		return
	}

	rows, _ := result.RowsAffected()
	if rows == 0 {
		http.Error(w, "User not found", http.StatusNotFound)
		return
	}

	w.WriteHeader(http.StatusOK)
}

func loginUser(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	username := r.URL.Query().Get("username")
	password := r.URL.Query().Get("password")

	if username == "" || password == "" {
		http.Error(w, "Invalid credentials", http.StatusBadRequest)
		return
	}

	var dbPassword string
	err := db.QueryRow(`SELECT password FROM users WHERE username = $1`, username).Scan(&dbPassword)

	if err == sql.ErrNoRows || dbPassword != password {
		http.Error(w, "Invalid credentials", http.StatusBadRequest)
		return
	}
	if err != nil {
		http.Error(w, "Database error", http.StatusInternalServerError)
		return
	}

	response := "Logged in successfully"
	writeJSON(w, http.StatusOK, response)
}

func pqArray(arr []string) interface{} {
	return arr
}

func main() {
	initDB()
	defer db.Close()

	http.HandleFunc("/pet", func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodPost:
			addPet(w, r)
		case http.MethodPut:
			updatePet(w, r)
		default:
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		}
	})

	http.HandleFunc("/pet/findByStatus", findPetsByStatus)
	http.HandleFunc("/pet/", func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/pet/") {
			http.Error(w, "Invalid path", http.StatusBadRequest)
			return
		}
		switch r.Method {
		case http.MethodGet:
			getPetById(w, r)
		case http.MethodDelete:
			deletePet(w, r)
		default:
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		}
	})

	http.HandleFunc("/store/order", placeOrder)
	http.HandleFunc("/store/order/", func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/store/order/") {
			http.Error(w, "Invalid path", http.StatusBadRequest)
			return
		}
		switch r.Method {
		case http.MethodGet:
			getOrderById(w, r)
		case http.MethodDelete:
			deleteOrder(w, r)
		default:
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		}
	})

	http.HandleFunc("/user", createUser)
	http.HandleFunc("/user/", func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/user/") {
			http.Error(w, "Invalid path", http.StatusBadRequest)
			return
		}
		switch r.Method {
		case http.MethodGet:
			getUserByName(w, r)
		case http.MethodPut:
			updateUser(w, r)
		case http.MethodDelete:
			deleteUser(w, r)
		default:
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		}
	})

	http.HandleFunc("/user/login", loginUser)

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	addr := "0.0.0.0:" + port
	fmt.Printf("Server starting on %s\n", addr)
	log.Fatal(http.ListenAndServe(addr, nil))
}