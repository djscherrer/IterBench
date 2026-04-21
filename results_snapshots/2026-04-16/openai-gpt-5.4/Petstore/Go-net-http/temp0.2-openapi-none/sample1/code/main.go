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
	db, err := openDBFromEnv()
	if err != nil {
		log.Fatal(err)
	}
	defer db.Close()

	if err := initDB(db); err != nil {
		log.Fatal(err)
	}

	app := &App{db: db}
	server := &http.Server{
		Addr:              net.JoinHostPort("0.0.0.0", getEnv("PORT", "5001")),
		Handler:           app.routes(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	log.Printf("listening on %s", server.Addr)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}

func openDBFromEnv() (*sql.DB, error) {
	host := os.Getenv("DB_HOST")
	port := getEnv("DB_PORT", "5432")
	user := os.Getenv("DB_USER")
	password := os.Getenv("DB_PASSWORD")
	name := os.Getenv("DB_NAME")

	if host == "" || user == "" || name == "" {
		return nil, fmt.Errorf("database environment variables DB_HOST, DB_USER, and DB_NAME must be set")
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
	db.SetMaxIdleConns(5)
	db.SetConnMaxLifetime(30 * time.Minute)

	return db, nil
}

func initDB(db *sql.DB) error {
	stmts := []string{
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
			quantity INTEGER NOT NULL DEFAULT 0,
			ship_date TIMESTAMPTZ,
			status TEXT,
			complete BOOLEAN NOT NULL DEFAULT FALSE,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)
		`,
		`
		CREATE TABLE IF NOT EXISTS users (
			id BIGINT PRIMARY KEY,
			username TEXT NOT NULL UNIQUE,
			first_name TEXT,
			last_name TEXT,
			email TEXT,
			password TEXT,
			phone TEXT,
			user_status INTEGER NOT NULL DEFAULT 0,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)
		`,
	}

	for _, stmt := range stmts {
		if _, err := db.Exec(stmt); err != nil {
			return err
		}
	}

	return nil
}

func (a *App) routes() http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("/pet/findByStatus", a.handleFindPetsByStatus)
	mux.HandleFunc("/pet/", a.handlePetByID)
	mux.HandleFunc("/pet", a.handlePet)

	mux.HandleFunc("/store/order/", a.handleOrderByID)
	mux.HandleFunc("/store/order", a.handleOrder)

	mux.HandleFunc("/user/login", a.handleUserLogin)
	mux.HandleFunc("/user/", a.handleUserByUsername)
	mux.HandleFunc("/user", a.handleUser)

	return mux
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

func (a *App) handlePetByID(w http.ResponseWriter, r *http.Request) {
	prefix := "/pet/"
	if !strings.HasPrefix(r.URL.Path, prefix) {
		notFound(w)
		return
	}

	idStr := strings.TrimPrefix(r.URL.Path, prefix)
	if idStr == "" || strings.Contains(idStr, "/") {
		notFound(w)
		return
	}

	petID, err := strconv.ParseInt(idStr, 10, 64)
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

	rows, err := a.db.Query(`SELECT id, name, photo_urls, COALESCE(status, '') FROM pets WHERE status = $1 ORDER BY id`, status)
	if err != nil {
		internalError(w, err)
		return
	}
	defer rows.Close()

	pets := make([]Pet, 0)
	for rows.Next() {
		var p Pet
		if err := rows.Scan(&p.ID, &p.Name, pqStringArray(&p.PhotoURLs), &p.Status); err != nil {
			internalError(w, err)
			return
		}
		pets = append(pets, p)
	}
	if err := rows.Err(); err != nil {
		internalError(w, err)
		return
	}

	writeJSON(w, http.StatusOK, pets)
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
		pet.ID, pet.Name, stringArrayValue(pet.PhotoURLs), nullIfEmpty(pet.Status),
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
		pet.ID, pet.Name, stringArrayValue(pet.PhotoURLs), nullIfEmpty(pet.Status),
	)
	if err != nil {
		internalError(w, err)
		return
	}

	affected, err := res.RowsAffected()
	if err != nil {
		internalError(w, err)
		return
	}
	if affected == 0 {
		writeStatus(w, http.StatusNotFound)
		return
	}

	writeJSON(w, http.StatusOK, pet)
}

func (a *App) getPetByID(w http.ResponseWriter, r *http.Request, petID int64) {
	var pet Pet
	err := a.db.QueryRow(
		`SELECT id, name, photo_urls, COALESCE(status, '') FROM pets WHERE id = $1`,
		petID,
	).Scan(&pet.ID, &pet.Name, pqStringArray(&pet.PhotoURLs), &pet.Status)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			writeStatus(w, http.StatusNotFound)
			return
		}
		internalError(w, err)
		return
	}

	writeJSON(w, http.StatusOK, pet)
}

func (a *App) deletePet(w http.ResponseWriter, r *http.Request, petID int64) {
	res, err := a.db.Exec(`DELETE FROM pets WHERE id = $1`, petID)
	if err != nil {
		internalError(w, err)
		return
	}

	affected, err := res.RowsAffected()
	if err != nil {
		internalError(w, err)
		return
	}
	if affected == 0 {
		writeStatus(w, http.StatusNotFound)
		return
	}

	writeStatus(w, http.StatusOK)
}

func (a *App) handleOrder(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}
	a.placeOrder(w, r)
}

func (a *App) handleOrderByID(w http.ResponseWriter, r *http.Request) {
	prefix := "/store/order/"
	if !strings.HasPrefix(r.URL.Path, prefix) {
		notFound(w)
		return
	}

	idStr := strings.TrimPrefix(r.URL.Path, prefix)
	if idStr == "" || strings.Contains(idStr, "/") {
		notFound(w)
		return
	}

	orderID, err := strconv.ParseInt(idStr, 10, 64)
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
		writeError(w, http.StatusBadRequest, "invalid status")
		return
	}

	var shipDateValue any
	if order.ShipDate != "" {
		t, err := time.Parse(time.RFC3339, order.ShipDate)
		if err != nil {
			writeError(w, http.StatusBadRequest, "invalid shipDate")
			return
		}
		shipDateValue = t
	}

	_, err := a.db.Exec(
		`INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete) VALUES ($1, $2, $3, $4, $5, $6)`,
		order.ID, order.PetID, order.Quantity, shipDateValue, nullIfEmpty(order.Status), order.Complete,
	)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid input")
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
			writeStatus(w, http.StatusNotFound)
			return
		}
		internalError(w, err)
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
		internalError(w, err)
		return
	}

	affected, err := res.RowsAffected()
	if err != nil {
		internalError(w, err)
		return
	}
	if affected == 0 {
		writeStatus(w, http.StatusNotFound)
		return
	}

	writeStatus(w, http.StatusOK)
}

func (a *App) handleUser(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}
	a.createUser(w, r)
}

func (a *App) handleUserByUsername(w http.ResponseWriter, r *http.Request) {
	prefix := "/user/"
	if !strings.HasPrefix(r.URL.Path, prefix) {
		notFound(w)
		return
	}

	username := strings.TrimPrefix(r.URL.Path, prefix)
	if username == "" || strings.Contains(username, "/") {
		notFound(w)
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

func (a *App) createUser(w http.ResponseWriter, r *http.Request) {
	var user User
	if err := decodeJSON(r, &user); err != nil {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	if strings.TrimSpace(user.Username) == "" {
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
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	writeJSON(w, http.StatusOK, user)
}

func (a *App) getUserByName(w http.ResponseWriter, r *http.Request, username string) {
	var user User
	var firstName, lastName, email, password, phone sql.NullString

	err := a.db.QueryRow(
		`SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username = $1`,
		username,
	).Scan(&user.ID, &user.Username, &firstName, &lastName, &email, &password, &phone, &user.UserStatus)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			writeStatus(w, http.StatusNotFound)
			return
		}
		internalError(w, err)
		return
	}

	if firstName.Valid {
		user.FirstName = firstName.String
	}
	if lastName.Valid {
		user.LastName = lastName.String
	}
	if email.Valid {
		user.Email = email.String
	}
	if password.Valid {
		user.Password = password.String
	}
	if phone.Valid {
		user.Phone = phone.String
	}

	writeJSON(w, http.StatusOK, user)
}

func (a *App) updateUser(w http.ResponseWriter, r *http.Request, username string) {
	var user User
	if err := decodeJSON(r, &user); err != nil {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	if strings.TrimSpace(user.Username) == "" {
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
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	affected, err := res.RowsAffected()
	if err != nil {
		internalError(w, err)
		return
	}
	if affected == 0 {
		writeStatus(w, http.StatusNotFound)
		return
	}

	var updated User
	var firstName, lastName, email, password, phone sql.NullString
	err = a.db.QueryRow(
		`SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username = $1`,
		user.Username,
	).Scan(&updated.ID, &updated.Username, &firstName, &updated.LastName, &email, &password, &phone, &updated.UserStatus)
	if err != nil {
		internalError(w, err)
		return
	}

	if firstName.Valid {
		updated.FirstName = firstName.String
	}
	if updated.LastName == "" && lastName.Valid {
		updated.LastName = lastName.String
	}
	if email.Valid {
		updated.Email = email.String
	}
	if password.Valid {
		updated.Password = password.String
	}
	if phone.Valid {
		updated.Phone = phone.String
	}

	writeJSON(w, http.StatusOK, updated)
}

func (a *App) deleteUser(w http.ResponseWriter, r *http.Request, username string) {
	res, err := a.db.Exec(`DELETE FROM users WHERE username = $1`, username)
	if err != nil {
		internalError(w, err)
		return
	}

	affected, err := res.RowsAffected()
	if err != nil {
		internalError(w, err)
		return
	}
	if affected == 0 {
		writeStatus(w, http.StatusNotFound)
		return
	}

	writeStatus(w, http.StatusOK)
}

func (a *App) handleUserLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w)
		return
	}

	username := r.URL.Query().Get("username")
	password := r.URL.Query().Get("password")
	if username == "" || password == "" {
		writeStatus(w, http.StatusBadRequest)
		return
	}

	var storedPassword string
	err := a.db.QueryRow(`SELECT COALESCE(password, '') FROM users WHERE username = $1`, username).Scan(&storedPassword)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			writeStatus(w, http.StatusBadRequest)
			return
		}
		internalError(w, err)
		return
	}

	if storedPassword != password {
		writeStatus(w, http.StatusBadRequest)
		return
	}

	writeJSON(w, http.StatusOK, "logged in user session")
}

func validatePet(p Pet) error {
	if strings.TrimSpace(p.Name) == "" {
		return fmt.Errorf("name is required")
	}
	if len(p.PhotoURLs) == 0 {
		return fmt.Errorf("photoUrls is required")
	}
	if p.Status != "" && !isValidPetStatus(p.Status) {
		return fmt.Errorf("invalid status")
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

func decodeJSON(r *http.Request, dst any) error {
	defer r.Body.Close()
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return err
	}
	if dec.More() {
		return fmt.Errorf("unexpected extra JSON")
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}

func writeStatus(w http.ResponseWriter, status int) {
	w.WriteHeader(status)
}

func methodNotAllowed(w http.ResponseWriter) {
	w.WriteHeader(http.StatusMethodNotAllowed)
}

func notFound(w http.ResponseWriter) {
	w.WriteHeader(http.StatusNotFound)
}

func internalError(w http.ResponseWriter, err error) {
	log.Printf("internal error: %v", err)
	writeStatus(w, http.StatusInternalServerError)
}

func getEnv(key, fallback string) string {
	value := os.Getenv(key)
	if value == "" {
		return fallback
	}
	return value
}

func nullIfEmpty(s string) any {
	if strings.TrimSpace(s) == "" {
		return nil
	}
	return s
}

type stringArray []string

func stringArrayValue(values []string) any {
	return stringArray(values)
}

func pqStringArray(dst *[]string) any {
	return &stringArrayScanner{dst: dst}
}

type stringArrayScanner struct {
	dst *[]string
}

func (s *stringArrayScanner) Scan(src any) error {
	if src == nil {
		*s.dst = nil
		return nil
	}

	var text string
	switch v := src.(type) {
	case string:
		text = v
	case []byte:
		text = string(v)
	default:
		return fmt.Errorf("unsupported array source type %T", src)
	}

	parsed, err := parsePostgresTextArray(text)
	if err != nil {
		return err
	}
	*s.dst = parsed
	return nil
}

func (a stringArray) Value() (driverValue any, err error) {
	escaped := make([]string, 0, len(a))
	for _, v := range a {
		v = strings.ReplaceAll(v, `\`, `\\`)
		v = strings.ReplaceAll(v, `"`, `\"`)
		escaped = append(escaped, `"`+v+`"`)
	}
	return "{" + strings.Join(escaped, ",") + "}", nil
}

func parsePostgresTextArray(input string) ([]string, error) {
	if input == "{}" {
		return []string{}, nil
	}
	if len(input) < 2 || input[0] != '{' || input[len(input)-1] != '}' {
		return nil, fmt.Errorf("invalid postgres array")
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

	if inQuotes || escaped {
		return nil, fmt.Errorf("invalid postgres array encoding")
	}

	result = append(result, current.String())
	return result, nil
}