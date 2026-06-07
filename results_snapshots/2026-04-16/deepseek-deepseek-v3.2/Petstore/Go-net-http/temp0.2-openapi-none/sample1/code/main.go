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
	if password == "" {
		password = "postgres"
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

	if err = db.Ping(); err != nil {
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
		quantity INT,
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
		user_status INT DEFAULT 0
	);
	`

	_, err = db.Exec(createTables)
	if err != nil {
		log.Fatal(err)
	}
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(map[string]string{"error": message})
}

func addPet(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "Method not allowed")
		return
	}

	var pet Pet
	if err := json.NewDecoder(r.Body).Decode(&pet); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	if pet.Name == "" || len(pet.PhotoUrls) == 0 {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	var id int64
	err := db.QueryRow(`
		INSERT INTO pets (name, photo_urls, status)
		VALUES ($1, $2, $3)
		RETURNING id
	`, pet.Name, pqArray(pet.PhotoUrls), pet.Status).Scan(&id)

	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}

	pet.ID = id
	writeJSON(w, http.StatusOK, pet)
}

func updatePet(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPut {
		writeError(w, http.StatusMethodNotAllowed, "Method not allowed")
		return
	}

	var pet Pet
	if err := json.NewDecoder(r.Body).Decode(&pet); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	if pet.ID == 0 {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	result, err := db.Exec(`
		UPDATE pets
		SET name = $1, photo_urls = $2, status = $3
		WHERE id = $4
	`, pet.Name, pqArray(pet.PhotoUrls), pet.Status, pet.ID)

	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
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
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "Method not allowed")
		return
	}

	status := r.URL.Query().Get("status")
	if status == "" {
		writeError(w, http.StatusBadRequest, "Missing status parameter")
		return
	}

	validStatus := map[string]bool{"available": true, "pending": true, "sold": true}
	if !validStatus[status] {
		writeError(w, http.StatusBadRequest, "Invalid status value")
		return
	}

	rows, err := db.Query(`
		SELECT id, name, photo_urls, status
		FROM pets
		WHERE status = $1
	`, status)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}
	defer rows.Close()

	var pets []Pet
	for rows.Next() {
		var pet Pet
		var photoUrls []string
		err := rows.Scan(&pet.ID, &pet.Name, &photoUrls, &pet.Status)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "Database error")
			return
		}
		pet.PhotoUrls = photoUrls
		pets = append(pets, pet)
	}

	writeJSON(w, http.StatusOK, pets)
}

func getPetById(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "Method not allowed")
		return
	}

	path := strings.TrimPrefix(r.URL.Path, "/pet/")
	id, err := strconv.ParseInt(path, 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid pet ID")
		return
	}

	var pet Pet
	var photoUrls []string
	err = db.QueryRow(`
		SELECT id, name, photo_urls, status
		FROM pets
		WHERE id = $1
	`, id).Scan(&pet.ID, &pet.Name, &photoUrls, &pet.Status)

	if err == sql.ErrNoRows {
		writeError(w, http.StatusNotFound, "Pet not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}

	pet.PhotoUrls = photoUrls
	writeJSON(w, http.StatusOK, pet)
}

func deletePet(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodDelete {
		writeError(w, http.StatusMethodNotAllowed, "Method not allowed")
		return
	}

	path := strings.TrimPrefix(r.URL.Path, "/pet/")
	id, err := strconv.ParseInt(path, 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid pet ID")
		return
	}

	result, err := db.Exec("DELETE FROM pets WHERE id = $1", id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}

	rows, _ := result.RowsAffected()
	if rows == 0 {
		writeError(w, http.StatusNotFound, "Pet not found")
		return
	}

	w.WriteHeader(http.StatusOK)
}

func placeOrder(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "Method not allowed")
		return
	}

	var order Order
	if err := json.NewDecoder(r.Body).Decode(&order); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	var id int64
	err := db.QueryRow(`
		INSERT INTO orders (pet_id, quantity, ship_date, status, complete)
		VALUES ($1, $2, $3, $4, $5)
		RETURNING id
	`, order.PetID, order.Quantity, order.ShipDate, order.Status, order.Complete).Scan(&id)

	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}

	order.ID = id
	writeJSON(w, http.StatusOK, order)
}

func getOrderById(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "Method not allowed")
		return
	}

	path := strings.TrimPrefix(r.URL.Path, "/store/order/")
	id, err := strconv.ParseInt(path, 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid order ID")
		return
	}

	var order Order
	err = db.QueryRow(`
		SELECT id, pet_id, quantity, ship_date, status, complete
		FROM orders
		WHERE id = $1
	`, id).Scan(&order.ID, &order.PetID, &order.Quantity, &order.ShipDate, &order.Status, &order.Complete)

	if err == sql.ErrNoRows {
		writeError(w, http.StatusNotFound, "Order not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}

	writeJSON(w, http.StatusOK, order)
}

func deleteOrder(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodDelete {
		writeError(w, http.StatusMethodNotAllowed, "Method not allowed")
		return
	}

	path := strings.TrimPrefix(r.URL.Path, "/store/order/")
	id, err := strconv.ParseInt(path, 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid order ID")
		return
	}

	result, err := db.Exec("DELETE FROM orders WHERE id = $1", id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}

	rows, _ := result.RowsAffected()
	if rows == 0 {
		writeError(w, http.StatusNotFound, "Order not found")
		return
	}

	w.WriteHeader(http.StatusOK)
}

func createUser(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "Method not allowed")
		return
	}

	var user User
	if err := json.NewDecoder(r.Body).Decode(&user); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	if user.Username == "" {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	var id int64
	err := db.QueryRow(`
		INSERT INTO users (username, first_name, last_name, email, password, phone, user_status)
		VALUES ($1, $2, $3, $4, $5, $6, $7)
		RETURNING id
	`, user.Username, user.FirstName, user.LastName, user.Email, user.Password, user.Phone, user.UserStatus).Scan(&id)

	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}

	user.ID = id
	writeJSON(w, http.StatusOK, user)
}

func getUserByName(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "Method not allowed")
		return
	}

	username := strings.TrimPrefix(r.URL.Path, "/user/")
	var user User
	err := db.QueryRow(`
		SELECT id, username, first_name, last_name, email, password, phone, user_status
		FROM users
		WHERE username = $1
	`, username).Scan(&user.ID, &user.Username, &user.FirstName, &user.LastName,
		&user.Email, &user.Password, &user.Phone, &user.UserStatus)

	if err == sql.ErrNoRows {
		writeError(w, http.StatusNotFound, "User not found")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}

	writeJSON(w, http.StatusOK, user)
}

func updateUser(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPut {
		writeError(w, http.StatusMethodNotAllowed, "Method not allowed")
		return
	}

	username := strings.TrimPrefix(r.URL.Path, "/user/")
	var user User
	if err := json.NewDecoder(r.Body).Decode(&user); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	result, err := db.Exec(`
		UPDATE users
		SET first_name = $1, last_name = $2, email = $3, password = $4, phone = $5, user_status = $6
		WHERE username = $7
	`, user.FirstName, user.LastName, user.Email, user.Password, user.Phone, user.UserStatus, username)

	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}

	rows, _ := result.RowsAffected()
	if rows == 0 {
		writeError(w, http.StatusNotFound, "User not found")
		return
	}

	user.Username = username
	writeJSON(w, http.StatusOK, user)
}

func deleteUser(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodDelete {
		writeError(w, http.StatusMethodNotAllowed, "Method not allowed")
		return
	}

	username := strings.TrimPrefix(r.URL.Path, "/user/")
	result, err := db.Exec("DELETE FROM users WHERE username = $1", username)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
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
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "Method not allowed")
		return
	}

	username := r.URL.Query().Get("username")
	password := r.URL.Query().Get("password")

	if username == "" || password == "" {
		writeError(w, http.StatusBadRequest, "Invalid credentials")
		return
	}

	var dbPassword string
	err := db.QueryRow("SELECT password FROM users WHERE username = $1", username).Scan(&dbPassword)
	if err == sql.ErrNoRows || dbPassword != password {
		writeError(w, http.StatusBadRequest, "Invalid credentials")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}

	writeJSON(w, http.StatusOK, "logged in")
}

func pqArray(arr []string) interface{} {
	return arr
}

func main() {
	initDB()

	http.HandleFunc("/pet", func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodPost:
			addPet(w, r)
		case http.MethodPut:
			updatePet(w, r)
		default:
			writeError(w, http.StatusMethodNotAllowed, "Method not allowed")
		}
	})

	http.HandleFunc("/pet/findByStatus", findPetsByStatus)
	http.HandleFunc("/pet/", func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/pet/") {
			writeError(w, http.StatusBadRequest, "Invalid pet ID")
			return
		}
		switch r.Method {
		case http.MethodGet:
			getPetById(w, r)
		case http.MethodDelete:
			deletePet(w, r)
		default:
			writeError(w, http.StatusMethodNotAllowed, "Method not allowed")
		}
	})

	http.HandleFunc("/store/order", placeOrder)
	http.HandleFunc("/store/order/", func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/store/order/") {
			writeError(w, http.StatusBadRequest, "Invalid order ID")
			return
		}
		switch r.Method {
		case http.MethodGet:
			getOrderById(w, r)
		case http.MethodDelete:
			deleteOrder(w, r)
		default:
			writeError(w, http.StatusMethodNotAllowed, "Method not allowed")
		}
	})

	http.HandleFunc("/user", createUser)
	http.HandleFunc("/user/", func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimPrefix(r.URL.Path, "/user/")
		if path == "login" {
			loginUser(w, r)
			return
		}
		if strings.HasSuffix(r.URL.Path, "/user/") {
			writeError(w, http.StatusBadRequest, "Invalid username")
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
			writeError(w, http.StatusMethodNotAllowed, "Method not allowed")
		}
	})

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	log.Printf("Server starting on 0.0.0.0:%s", port)
	log.Fatal(http.ListenAndServe("0.0.0.0:"+port, nil))
}