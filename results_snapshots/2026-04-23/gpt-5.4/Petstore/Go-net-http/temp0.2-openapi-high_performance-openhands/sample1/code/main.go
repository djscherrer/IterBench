package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/lib/pq"
)

const maxBodyBytes int64 = 1 << 20

type App struct {
	db *sql.DB
}

type Pet struct {
	ID        int64    `json:"id,omitempty"`
	Name      string   `json:"name"`
	PhotoURLs []string `json:"photoUrls"`
	Status    string   `json:"status,omitempty"`
}

type Order struct {
	ID       int64      `json:"id,omitempty"`
	PetID    int64      `json:"petId,omitempty"`
	Quantity int        `json:"quantity,omitempty"`
	ShipDate *time.Time `json:"shipDate,omitempty"`
	Status   string     `json:"status,omitempty"`
	Complete bool       `json:"complete"`
}

type User struct {
	ID         int64  `json:"id,omitempty"`
	Username   string `json:"username"`
	FirstName  string `json:"firstName,omitempty"`
	LastName   string `json:"lastName,omitempty"`
	Email      string `json:"email,omitempty"`
	Password   string `json:"password,omitempty"`
	Phone      string `json:"phone,omitempty"`
	UserStatus int    `json:"userStatus,omitempty"`
}

func main() {
	db, err := openDB()
	if err != nil {
		log.Fatalf("open database: %v", err)
	}
	defer db.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	if err := initDB(ctx, db); err != nil {
		log.Fatalf("initialize database: %v", err)
	}

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	app := &App{db: db}
	srv := &http.Server{
		Addr:              "0.0.0.0:" + port,
		Handler:           app.routes(),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}

	log.Printf("listening on %s", srv.Addr)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("server error: %v", err)
	}
}

func openDB() (*sql.DB, error) {
	host := os.Getenv("DB_HOST")
	port := os.Getenv("DB_PORT")
	user := os.Getenv("DB_USER")
	password := os.Getenv("DB_PASSWORD")
	name := os.Getenv("DB_NAME")
	if host == "" || port == "" || user == "" || name == "" {
		return nil, errors.New("database configuration is incomplete")
	}

	dsn := fmt.Sprintf(
		"host=%s port=%s user=%s password=%s dbname=%s sslmode=disable connect_timeout=5",
		host,
		port,
		user,
		password,
		name,
	)

	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}

	maxOpen := runtime.GOMAXPROCS(0) * 8
	if maxOpen < 32 {
		maxOpen = 32
	}
	db.SetMaxOpenConns(maxOpen)
	db.SetMaxIdleConns(maxOpen / 2)
	db.SetConnMaxIdleTime(5 * time.Minute)
	db.SetConnMaxLifetime(30 * time.Minute)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}

	return db, nil
}

func initDB(ctx context.Context, db *sql.DB) error {
	const schema = `
CREATE TABLE IF NOT EXISTS pets (
	id BIGSERIAL PRIMARY KEY,
	name TEXT NOT NULL,
	photo_urls TEXT[] NOT NULL DEFAULT '{}'::TEXT[],
	status TEXT,
	CONSTRAINT pets_status_check CHECK (status IS NULL OR status IN ('available', 'pending', 'sold'))
);
CREATE INDEX IF NOT EXISTS idx_pets_status ON pets (status);

CREATE TABLE IF NOT EXISTS orders (
	id BIGSERIAL PRIMARY KEY,
	pet_id BIGINT NOT NULL,
	quantity INTEGER NOT NULL DEFAULT 0,
	ship_date TIMESTAMPTZ,
	status TEXT,
	complete BOOLEAN NOT NULL DEFAULT FALSE,
	CONSTRAINT orders_status_check CHECK (status IS NULL OR status IN ('placed', 'approved', 'delivered'))
);
CREATE INDEX IF NOT EXISTS idx_orders_pet_id ON orders (pet_id);

CREATE TABLE IF NOT EXISTS users (
	id BIGSERIAL PRIMARY KEY,
	username TEXT NOT NULL UNIQUE,
	first_name TEXT NOT NULL DEFAULT '',
	last_name TEXT NOT NULL DEFAULT '',
	email TEXT NOT NULL DEFAULT '',
	password TEXT NOT NULL DEFAULT '',
	phone TEXT NOT NULL DEFAULT '',
	user_status INTEGER NOT NULL DEFAULT 0
);
`
	_, err := db.ExecContext(ctx, schema)
	return err
}

func (a *App) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /pet", a.addPet)
	mux.HandleFunc("PUT /pet", a.updatePet)
	mux.HandleFunc("GET /pet/findByStatus", a.findPetsByStatus)
	mux.HandleFunc("GET /pet/{petId}", a.getPetByID)
	mux.HandleFunc("DELETE /pet/{petId}", a.deletePet)
	mux.HandleFunc("POST /store/order", a.placeOrder)
	mux.HandleFunc("GET /store/order/{orderId}", a.getOrderByID)
	mux.HandleFunc("DELETE /store/order/{orderId}", a.deleteOrder)
	mux.HandleFunc("POST /user", a.createUser)
	mux.HandleFunc("GET /user/login", a.loginUser)
	mux.HandleFunc("GET /user/{username}", a.getUserByName)
	mux.HandleFunc("PUT /user/{username}", a.updateUser)
	mux.HandleFunc("DELETE /user/{username}", a.deleteUser)
	return mux
}

func (a *App) addPet(w http.ResponseWriter, r *http.Request) {
	var pet Pet
	if !decodeJSON(w, r, &pet) {
		return
	}
	if err := validatePet(pet); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	var row rowScanner
	if pet.ID > 0 {
		row = a.db.QueryRowContext(ctx, `
INSERT INTO pets (id, name, photo_urls, status)
VALUES ($1, $2, $3, $4)
RETURNING id, name, photo_urls, status`, pet.ID, pet.Name, pq.Array(pet.PhotoURLs), nullableString(pet.Status))
	} else {
		row = a.db.QueryRowContext(ctx, `
INSERT INTO pets (name, photo_urls, status)
VALUES ($1, $2, $3)
RETURNING id, name, photo_urls, status`, pet.Name, pq.Array(pet.PhotoURLs), nullableString(pet.Status))
	}

	created, err := scanPet(row)
	if err != nil {
		writeDBError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, created)
}

func (a *App) updatePet(w http.ResponseWriter, r *http.Request) {
	var pet Pet
	if !decodeJSON(w, r, &pet) {
		return
	}
	if pet.ID <= 0 {
		http.Error(w, "id is required", http.StatusBadRequest)
		return
	}
	if err := validatePet(pet); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	updated, err := scanPet(a.db.QueryRowContext(ctx, `
UPDATE pets
SET name = $2, photo_urls = $3, status = $4
WHERE id = $1
RETURNING id, name, photo_urls, status`, pet.ID, pet.Name, pq.Array(pet.PhotoURLs), nullableString(pet.Status)))
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			http.Error(w, "pet not found", http.StatusNotFound)
			return
		}
		writeDBError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, updated)
}

func (a *App) findPetsByStatus(w http.ResponseWriter, r *http.Request) {
	status := r.URL.Query().Get("status")
	if !validPetStatus(status) || status == "" {
		http.Error(w, "invalid status", http.StatusBadRequest)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	rows, err := a.db.QueryContext(ctx, `
SELECT id, name, photo_urls, status
FROM pets
WHERE status = $1
ORDER BY id`, status)
	if err != nil {
		writeDBError(w, err)
		return
	}
	defer rows.Close()

	pets := make([]Pet, 0, 16)
	for rows.Next() {
		pet, err := scanPet(rows)
		if err != nil {
			writeDBError(w, err)
			return
		}
		pets = append(pets, pet)
	}
	if err := rows.Err(); err != nil {
		writeDBError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, pets)
}

func (a *App) getPetByID(w http.ResponseWriter, r *http.Request) {
	petID, ok := parseIDParam(w, r, "petId")
	if !ok {
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	pet, err := scanPet(a.db.QueryRowContext(ctx, `
SELECT id, name, photo_urls, status
FROM pets
WHERE id = $1`, petID))
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			http.Error(w, "pet not found", http.StatusNotFound)
			return
		}
		writeDBError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, pet)
}

func (a *App) deletePet(w http.ResponseWriter, r *http.Request) {
	petID, ok := parseIDParam(w, r, "petId")
	if !ok {
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	result, err := a.db.ExecContext(ctx, `DELETE FROM pets WHERE id = $1`, petID)
	if err != nil {
		writeDBError(w, err)
		return
	}
	if !hasRows(result) {
		http.Error(w, "pet not found", http.StatusNotFound)
		return
	}
	w.WriteHeader(http.StatusOK)
}

func (a *App) placeOrder(w http.ResponseWriter, r *http.Request) {
	var order Order
	if !decodeJSON(w, r, &order) {
		return
	}
	if err := validateOrder(order); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	var row rowScanner
	if order.ID > 0 {
		row = a.db.QueryRowContext(ctx, `
INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete)
VALUES ($1, $2, $3, $4, $5, $6)
RETURNING id, pet_id, quantity, ship_date, status, complete`, order.ID, order.PetID, order.Quantity, nullableTime(order.ShipDate), nullableString(order.Status), order.Complete)
	} else {
		row = a.db.QueryRowContext(ctx, `
INSERT INTO orders (pet_id, quantity, ship_date, status, complete)
VALUES ($1, $2, $3, $4, $5)
RETURNING id, pet_id, quantity, ship_date, status, complete`, order.PetID, order.Quantity, nullableTime(order.ShipDate), nullableString(order.Status), order.Complete)
	}

	created, err := scanOrder(row)
	if err != nil {
		writeDBError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, created)
}

func (a *App) getOrderByID(w http.ResponseWriter, r *http.Request) {
	orderID, ok := parseIDParam(w, r, "orderId")
	if !ok {
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	order, err := scanOrder(a.db.QueryRowContext(ctx, `
SELECT id, pet_id, quantity, ship_date, status, complete
FROM orders
WHERE id = $1`, orderID))
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			http.Error(w, "order not found", http.StatusNotFound)
			return
		}
		writeDBError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, order)
}

func (a *App) deleteOrder(w http.ResponseWriter, r *http.Request) {
	orderID, ok := parseIDParam(w, r, "orderId")
	if !ok {
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	result, err := a.db.ExecContext(ctx, `DELETE FROM orders WHERE id = $1`, orderID)
	if err != nil {
		writeDBError(w, err)
		return
	}
	if !hasRows(result) {
		http.Error(w, "order not found", http.StatusNotFound)
		return
	}
	w.WriteHeader(http.StatusOK)
}

func (a *App) createUser(w http.ResponseWriter, r *http.Request) {
	var user User
	if !decodeJSON(w, r, &user) {
		return
	}
	if err := validateUser(user); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	var row rowScanner
	if user.ID > 0 {
		row = a.db.QueryRowContext(ctx, `
INSERT INTO users (id, username, first_name, last_name, email, password, phone, user_status)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
RETURNING id, username, first_name, last_name, email, password, phone, user_status`, user.ID, user.Username, user.FirstName, user.LastName, user.Email, user.Password, user.Phone, user.UserStatus)
	} else {
		row = a.db.QueryRowContext(ctx, `
INSERT INTO users (username, first_name, last_name, email, password, phone, user_status)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING id, username, first_name, last_name, email, password, phone, user_status`, user.Username, user.FirstName, user.LastName, user.Email, user.Password, user.Phone, user.UserStatus)
	}

	created, err := scanUser(row)
	if err != nil {
		writeDBError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, created)
}

func (a *App) getUserByName(w http.ResponseWriter, r *http.Request) {
	username := strings.TrimSpace(r.PathValue("username"))
	if username == "" {
		http.Error(w, "username is required", http.StatusBadRequest)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	user, err := scanUser(a.db.QueryRowContext(ctx, `
SELECT id, username, first_name, last_name, email, password, phone, user_status
FROM users
WHERE username = $1`, username))
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			http.Error(w, "user not found", http.StatusNotFound)
			return
		}
		writeDBError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, user)
}

func (a *App) updateUser(w http.ResponseWriter, r *http.Request) {
	pathUsername := strings.TrimSpace(r.PathValue("username"))
	if pathUsername == "" {
		http.Error(w, "username is required", http.StatusBadRequest)
		return
	}

	var user User
	if !decodeJSON(w, r, &user) {
		return
	}
	if user.Username == "" {
		user.Username = pathUsername
	}
	if err := validateUser(user); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	updated, err := scanUser(a.db.QueryRowContext(ctx, `
UPDATE users
SET username = $2,
	first_name = $3,
	last_name = $4,
	email = $5,
	password = $6,
	phone = $7,
	user_status = $8
WHERE username = $1
RETURNING id, username, first_name, last_name, email, password, phone, user_status`, pathUsername, user.Username, user.FirstName, user.LastName, user.Email, user.Password, user.Phone, user.UserStatus))
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			http.Error(w, "user not found", http.StatusNotFound)
			return
		}
		writeDBError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, updated)
}

func (a *App) deleteUser(w http.ResponseWriter, r *http.Request) {
	username := strings.TrimSpace(r.PathValue("username"))
	if username == "" {
		http.Error(w, "username is required", http.StatusBadRequest)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	result, err := a.db.ExecContext(ctx, `DELETE FROM users WHERE username = $1`, username)
	if err != nil {
		writeDBError(w, err)
		return
	}
	if !hasRows(result) {
		http.Error(w, "user not found", http.StatusNotFound)
		return
	}
	w.WriteHeader(http.StatusOK)
}

func (a *App) loginUser(w http.ResponseWriter, r *http.Request) {
	username := strings.TrimSpace(r.URL.Query().Get("username"))
	password := r.URL.Query().Get("password")
	if username == "" || password == "" {
		http.Error(w, "invalid credentials", http.StatusBadRequest)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	var exists bool
	if err := a.db.QueryRowContext(ctx, `
SELECT EXISTS(
	SELECT 1
	FROM users
	WHERE username = $1 AND password = $2
)`, username, password).Scan(&exists); err != nil {
		writeDBError(w, err)
		return
	}
	if !exists {
		http.Error(w, "invalid credentials", http.StatusBadRequest)
		return
	}
	writeJSON(w, http.StatusOK, "logged in")
}

type rowScanner interface {
	Scan(dest ...any) error
}

func scanPet(scanner rowScanner) (Pet, error) {
	var pet Pet
	var photoURLs []string
	var status sql.NullString
	if err := scanner.Scan(&pet.ID, &pet.Name, pq.Array(&photoURLs), &status); err != nil {
		return Pet{}, err
	}
	pet.PhotoURLs = photoURLs
	if status.Valid {
		pet.Status = status.String
	}
	return pet, nil
}

func scanOrder(scanner rowScanner) (Order, error) {
	var order Order
	var shipDate sql.NullTime
	var status sql.NullString
	if err := scanner.Scan(&order.ID, &order.PetID, &order.Quantity, &shipDate, &status, &order.Complete); err != nil {
		return Order{}, err
	}
	if shipDate.Valid {
		t := shipDate.Time.UTC()
		order.ShipDate = &t
	}
	if status.Valid {
		order.Status = status.String
	}
	return order, nil
}

func scanUser(scanner rowScanner) (User, error) {
	var user User
	if err := scanner.Scan(&user.ID, &user.Username, &user.FirstName, &user.LastName, &user.Email, &user.Password, &user.Phone, &user.UserStatus); err != nil {
		return User{}, err
	}
	return user, nil
}

func decodeJSON(w http.ResponseWriter, r *http.Request, dst any) bool {
	defer r.Body.Close()
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxBodyBytes))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(dst); err != nil {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return false
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(value); err != nil {
		log.Printf("encode response: %v", err)
	}
}

func parseIDParam(w http.ResponseWriter, r *http.Request, name string) (int64, bool) {
	value := strings.TrimSpace(r.PathValue(name))
	id, err := strconv.ParseInt(value, 10, 64)
	if err != nil || id <= 0 {
		http.Error(w, "invalid id", http.StatusBadRequest)
		return 0, false
	}
	return id, true
}

func validatePet(pet Pet) error {
	if strings.TrimSpace(pet.Name) == "" {
		return errors.New("name is required")
	}
	if pet.PhotoURLs == nil {
		return errors.New("photoUrls is required")
	}
	if !validPetStatus(pet.Status) {
		return errors.New("invalid status")
	}
	return nil
}

func validPetStatus(status string) bool {
	switch status {
	case "", "available", "pending", "sold":
		return true
	default:
		return false
	}
}

func validateOrder(order Order) error {
	if order.PetID <= 0 {
		return errors.New("petId is required")
	}
	if order.Quantity < 0 {
		return errors.New("quantity must be non-negative")
	}
	if !validOrderStatus(order.Status) {
		return errors.New("invalid status")
	}
	return nil
}

func validOrderStatus(status string) bool {
	switch status {
	case "", "placed", "approved", "delivered":
		return true
	default:
		return false
	}
}

func validateUser(user User) error {
	if strings.TrimSpace(user.Username) == "" {
		return errors.New("username is required")
	}
	return nil
}

func nullableString(value string) any {
	if value == "" {
		return nil
	}
	return value
}

func nullableTime(value *time.Time) any {
	if value == nil {
		return nil
	}
	return value.UTC()
}

func hasRows(result sql.Result) bool {
	rows, err := result.RowsAffected()
	return err == nil && rows > 0
}

func writeDBError(w http.ResponseWriter, err error) {
	var pqErr *pq.Error
	if errors.As(err, &pqErr) {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}
	log.Printf("database error: %v", err)
	http.Error(w, "internal server error", http.StatusInternalServerError)
}
