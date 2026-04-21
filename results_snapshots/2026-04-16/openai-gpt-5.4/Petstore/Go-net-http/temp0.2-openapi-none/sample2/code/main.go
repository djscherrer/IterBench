package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log"
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
		log.Fatalf("failed to connect database: %v", err)
	}
	defer db.Close()

	if err := initDB(db); err != nil {
		log.Fatalf("failed to initialize database: %v", err)
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

	addr := "0.0.0.0:" + port
	log.Printf("server listening on %s", addr)
	if err := http.ListenAndServe(addr, loggingMiddleware(mux)); err != nil {
		log.Fatalf("server failed: %v", err)
	}
}

func openDB() (*sql.DB, error) {
	host := os.Getenv("DB_HOST")
	port := os.Getenv("DB_PORT")
	user := os.Getenv("DB_USER")
	password := os.Getenv("DB_PASSWORD")
	name := os.Getenv("DB_NAME")

	if host == "" || port == "" || user == "" || name == "" {
		return nil, errors.New("DB_HOST, DB_PORT, DB_USER, and DB_NAME must be set")
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
		return nil, err
	}

	db.SetMaxOpenConns(10)
	db.SetMaxIdleConns(5)
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
			complete BOOLEAN,
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

func (a *App) handlePet(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodPost:
		a.addPet(w, r)
	case http.MethodPut:
		a.updatePet(w, r)
	default:
		methodNotAllowed(w)
	}
}

func (a *App) handleFindPetsByStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w)
		return
	}
	status := r.URL.Query().Get("status")
	if !isValidPetStatus(status) {
		writeError(w, http.StatusBadRequest, "invalid status")
		return
	}

	rows, err := a.db.Query(`SELECT id, name, photo_urls, status FROM pets WHERE status = $1 ORDER BY id`, status)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to query pets")
		return
	}
	defer rows.Close()

	pets := make([]Pet, 0)
	for rows.Next() {
		var p Pet
		if err := rows.Scan(&p.ID, &p.Name, pqStringArrayScan(&p.PhotoURLs), &p.Status); err != nil {
			writeError(w, http.StatusInternalServerError, "failed to scan pet")
			return
		}
		pets = append(pets, p)
	}

	if err := rows.Err(); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to read pets")
		return
	}

	writeJSON(w, http.StatusOK, pets)
}

func (a *App) handlePetByID(w http.ResponseWriter, r *http.Request) {
	petIDStr := strings.TrimPrefix(r.URL.Path, "/pet/")
	if petIDStr == "" || strings.Contains(petIDStr, "/") {
		http.NotFound(w, r)
		return
	}

	petID, err := strconv.ParseInt(petIDStr, 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid petId")
		return
	}

	switch r.Method {
	case http.MethodGet:
		a.getPetByID(w, r, petID)
	case http.MethodDelete:
		a.deletePet(w, r, petID)
	default:
		methodNotAllowed(w)
	}
}

func (a *App) addPet(w http.ResponseWriter, r *http.Request) {
	var pet Pet
	if err := decodeJSON(r, &pet); err != nil {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	if err := validatePet(pet); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	if pet.ID == 0 {
		pet.ID = time.Now().UnixNano()
	}

	_, err := a.db.Exec(
		`INSERT INTO pets (id, name, photo_urls, status) VALUES ($1, $2, $3, $4)`,
		pet.ID, pet.Name, pqStringArrayValue(pet.PhotoURLs), nullIfEmpty(pet.Status),
	)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	writeJSON(w, http.StatusOK, pet)
}

func (a *App) updatePet(w http.ResponseWriter, r *http.Request) {
	var pet Pet
	if err := decodeJSON(r, &pet); err != nil {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	if pet.ID == 0 {
		writeError(w, http.StatusBadRequest, "id is required")
		return
	}

	if err := validatePet(pet); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	res, err := a.db.Exec(
		`UPDATE pets SET name = $2, photo_urls = $3, status = $4 WHERE id = $1`,
		pet.ID, pet.Name, pqStringArrayValue(pet.PhotoURLs), nullIfEmpty(pet.Status),
	)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to update pet")
		return
	}

	rows, err := res.RowsAffected()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to update pet")
		return
	}
	if rows == 0 {
		writeError(w, http.StatusNotFound, "pet not found")
		return
	}

	writeJSON(w, http.StatusOK, pet)
}

func (a *App) getPetByID(w http.ResponseWriter, r *http.Request, petID int64) {
	var pet Pet
	err := a.db.QueryRow(
		`SELECT id, name, photo_urls, status FROM pets WHERE id = $1`,
		petID,
	).Scan(&pet.ID, &pet.Name, pqStringArrayScan(&pet.PhotoURLs), &pet.Status)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			writeError(w, http.StatusNotFound, "pet not found")
			return
		}
		writeError(w, http.StatusInternalServerError, "failed to fetch pet")
		return
	}

	writeJSON(w, http.StatusOK, pet)
}

func (a *App) deletePet(w http.ResponseWriter, r *http.Request, petID int64) {
	res, err := a.db.Exec(`DELETE FROM pets WHERE id = $1`, petID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to delete pet")
		return
	}

	rows, err := res.RowsAffected()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to delete pet")
		return
	}
	if rows == 0 {
		writeError(w, http.StatusNotFound, "pet not found")
		return
	}

	w.WriteHeader(http.StatusOK)
}

func (a *App) handleOrder(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}
	a.placeOrder(w, r)
}

func (a *App) handleOrderByID(w http.ResponseWriter, r *http.Request) {
	orderIDStr := strings.TrimPrefix(r.URL.Path, "/store/order/")
	if orderIDStr == "" || strings.Contains(orderIDStr, "/") {
		http.NotFound(w, r)
		return
	}

	orderID, err := strconv.ParseInt(orderIDStr, 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid orderId")
		return
	}

	switch r.Method {
	case http.MethodGet:
		a.getOrderByID(w, r, orderID)
	case http.MethodDelete:
		a.deleteOrder(w, r, orderID)
	default:
		methodNotAllowed(w)
	}
}

func (a *App) placeOrder(w http.ResponseWriter, r *http.Request) {
	var order Order
	if err := decodeJSON(r, &order); err != nil {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	if order.ID == 0 {
		order.ID = time.Now().UnixNano()
	}
	if order.Status != "" && !isValidOrderStatus(order.Status) {
		writeError(w, http.StatusBadRequest, "invalid order status")
		return
	}

	var shipDate interface{}
	if order.ShipDate != "" {
		t, err := time.Parse(time.RFC3339, order.ShipDate)
		if err != nil {
			writeError(w, http.StatusBadRequest, "invalid shipDate")
			return
		}
		shipDate = t
	}

	_, err := a.db.Exec(
		`INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete) VALUES ($1, $2, $3, $4, $5, $6)`,
		order.ID, order.PetID, order.Quantity, shipDate, nullIfEmpty(order.Status), order.Complete,
	)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to place order")
		return
	}

	writeJSON(w, http.StatusOK, order)
}

func (a *App) getOrderByID(w http.ResponseWriter, r *http.Request, orderID int64) {
	var order Order
	var shipDate sql.NullTime
	var status sql.NullString

	err := a.db.QueryRow(
		`SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id = $1`,
		orderID,
	).Scan(&order.ID, &order.PetID, &order.Quantity, &shipDate, &status, &order.Complete)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			writeError(w, http.StatusNotFound, "order not found")
			return
		}
		writeError(w, http.StatusInternalServerError, "failed to fetch order")
		return
	}

	if shipDate.Valid {
		order.ShipDate = shipDate.Time.UTC().Format(time.RFC3339)
	}
	if status.Valid {
		order.Status = status.String
	}

	writeJSON(w, http.StatusOK, order)
}

func (a *App) deleteOrder(w http.ResponseWriter, r *http.Request, orderID int64) {
	res, err := a.db.Exec(`DELETE FROM orders WHERE id = $1`, orderID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to delete order")
		return
	}

	rows, err := res.RowsAffected()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to delete order")
		return
	}
	if rows == 0 {
		writeError(w, http.StatusNotFound, "order not found")
		return
	}

	w.WriteHeader(http.StatusOK)
}

func (a *App) handleUser(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}
	a.createUser(w, r)
}

func (a *App) handleUserByUsername(w http.ResponseWriter, r *http.Request) {
	username := strings.TrimPrefix(r.URL.Path, "/user/")
	if username == "" || strings.Contains(username, "/") {
		http.NotFound(w, r)
		return
	}

	switch r.Method {
	case http.MethodGet:
		a.getUserByName(w, r, username)
	case http.MethodPut:
		a.updateUser(w, r, username)
	case http.MethodDelete:
		a.deleteUser(w, r, username)
	default:
		methodNotAllowed(w)
	}
}

func (a *App) handleLoginUser(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w)
		return
	}

	username := r.URL.Query().Get("username")
	password := r.URL.Query().Get("password")
	if username == "" || password == "" {
		writeError(w, http.StatusBadRequest, "invalid credentials")
		return
	}

	var storedPassword string
	err := a.db.QueryRow(`SELECT password FROM users WHERE username = $1`, username).Scan(&storedPassword)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			writeError(w, http.StatusBadRequest, "invalid credentials")
			return
		}
		writeError(w, http.StatusInternalServerError, "failed to login")
		return
	}

	if storedPassword != password {
		writeError(w, http.StatusBadRequest, "invalid credentials")
		return
	}

	writeJSON(w, http.StatusOK, "logged in")
}

func (a *App) createUser(w http.ResponseWriter, r *http.Request) {
	var user User
	if err := decodeJSON(r, &user); err != nil {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	if user.Username == "" {
		writeError(w, http.StatusBadRequest, "username is required")
		return
	}

	if user.ID == 0 {
		user.ID = time.Now().UnixNano()
	}

	_, err := a.db.Exec(
		`INSERT INTO users (id, username, first_name, last_name, email, password, phone, user_status)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
		user.ID, user.Username, nullIfEmpty(user.FirstName), nullIfEmpty(user.LastName),
		nullIfEmpty(user.Email), nullIfEmpty(user.Password), nullIfEmpty(user.Phone), user.UserStatus,
	)
	if err != nil {
		writeError(w, http.StatusBadRequest, "failed to create user")
		return
	}

	writeJSON(w, http.StatusOK, user)
}

func (a *App) getUserByName(w http.ResponseWriter, r *http.Request, username string) {
	var user User
	err := a.db.QueryRow(
		`SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username = $1`,
		username,
	).Scan(
		&user.ID, &user.Username, &user.FirstName, &user.LastName, &user.Email, &user.Password, &user.Phone, &user.UserStatus,
	)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			writeError(w, http.StatusNotFound, "user not found")
			return
		}
		writeError(w, http.StatusInternalServerError, "failed to fetch user")
		return
	}

	writeJSON(w, http.StatusOK, user)
}

func (a *App) updateUser(w http.ResponseWriter, r *http.Request, username string) {
	var user User
	if err := decodeJSON(r, &user); err != nil {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	if user.Username == "" {
		user.Username = username
	}

	res, err := a.db.Exec(
		`UPDATE users
		 SET username = $2, first_name = $3, last_name = $4, email = $5, password = $6, phone = $7, user_status = $8
		 WHERE username = $1`,
		username, user.Username, nullIfEmpty(user.FirstName), nullIfEmpty(user.LastName),
		nullIfEmpty(user.Email), nullIfEmpty(user.Password), nullIfEmpty(user.Phone), user.UserStatus,
	)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to update user")
		return
	}

	rows, err := res.RowsAffected()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to update user")
		return
	}
	if rows == 0 {
		writeError(w, http.StatusNotFound, "user not found")
		return
	}

	var updated User
	err = a.db.QueryRow(
		`SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username = $1`,
		user.Username,
	).Scan(
		&updated.ID, &updated.Username, &updated.FirstName, &updated.LastName, &updated.Email, &updated.Password, &updated.Phone, &updated.UserStatus,
	)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to fetch updated user")
		return
	}

	writeJSON(w, http.StatusOK, updated)
}

func (a *App) deleteUser(w http.ResponseWriter, r *http.Request, username string) {
	res, err := a.db.Exec(`DELETE FROM users WHERE username = $1`, username)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to delete user")
		return
	}

	rows, err := res.RowsAffected()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to delete user")
		return
	}
	if rows == 0 {
		writeError(w, http.StatusNotFound, "user not found")
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
		return errors.New("invalid pet status")
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

func decodeJSON(r *http.Request, dst interface{}) error {
	defer r.Body.Close()
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	return dec.Decode(dst)
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func methodNotAllowed(w http.ResponseWriter) {
	w.WriteHeader(http.StatusMethodNotAllowed)
}

func nullIfEmpty(s string) interface{} {
	if s == "" {
		return nil
	}
	return s
}

type stringArrayValue []string

func pqStringArrayValue(v []string) driverValuer {
	return stringArrayValue(v)
}

func (a stringArrayValue) Value() (interface{}, error) {
	escaped := make([]string, 0, len(a))
	for _, s := range a {
		s = strings.ReplaceAll(s, `\`, `\\`)
		s = strings.ReplaceAll(s, `"`, `\"`)
		escaped = append(escaped, `"`+s+`"`)
	}
	return "{" + strings.Join(escaped, ",") + "}", nil
}

type stringArrayScanner struct {
	target *[]string
}

func pqStringArrayScan(target *[]string) sql.Scanner {
	return &stringArrayScanner{target: target}
}

func (s *stringArrayScanner) Scan(src interface{}) error {
	if src == nil {
		*s.target = nil
		return nil
	}

	var raw string
	switch v := src.(type) {
	case string:
		raw = v
	case []byte:
		raw = string(v)
	default:
		return fmt.Errorf("unsupported array source type %T", src)
	}

	parsed, err := parsePostgresTextArray(raw)
	if err != nil {
		return err
	}
	*s.target = parsed
	return nil
}

func parsePostgresTextArray(input string) ([]string, error) {
	if input == "{}" {
		return []string{}, nil
	}
	if len(input) < 2 || input[0] != '{' || input[len(input)-1] != '}' {
		return nil, errors.New("invalid postgres array format")
	}

	content := input[1 : len(input)-1]
	if content == "" {
		return []string{}, nil
	}

	var result []string
	var current strings.Builder
	inQuotes := false
	escaped := false

	for i := 0; i < len(content); i++ {
		ch := content[i]

		if escaped {
			current.WriteByte(ch)
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

		current.WriteByte(ch)
	}

	if inQuotes {
		return nil, errors.New("unterminated quoted string in array")
	}

	result = append(result, current.String())
	return result, nil
}

type driverValuer interface {
	Value() (interface{}, error)
}

func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		log.Printf("%s %s %s", r.Method, r.URL.Path, time.Since(start))
	})
}