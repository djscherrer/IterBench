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

	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

type Click struct {
	ID        string    `json:"id"`
	Timestamp time.Time `json:"timestamp"`
}

type ClickRequest struct {
	ID string `json:"id"`
}

var db *sql.DB

func main() {
	initDB()
	http.HandleFunc("/click", clickHandler)
	http.HandleFunc("/clicks", clicksHandler)

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	addr := fmt.Sprintf("0.0.0.0:%s", port)
	log.Printf("Server starting on %s", addr)
	log.Fatal(http.ListenAndServe(addr, nil))
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
		log.Fatal(err)
	}

	err = db.Ping()
	if err != nil {
		log.Fatal(err)
	}

	createTableSQL := `
	CREATE TABLE IF NOT EXISTS clicks (
		id UUID PRIMARY KEY,
		timestamp TIMESTAMP NOT NULL
	);`
	_, err = db.Exec(createTableSQL)
	if err != nil {
		log.Fatal(err)
	}
	log.Println("Database initialized")
}

func clickHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req ClickRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid request body", http.StatusBadRequest)
		return
	}

	if req.ID == "" {
		req.ID = uuid.New().String()
	}

	_, err := uuid.Parse(req.ID)
	if err != nil {
		http.Error(w, "Invalid UUID format", http.StatusBadRequest)
		return
	}

	_, err = db.Exec("INSERT INTO clicks (id, timestamp) VALUES ($1, $2)", req.ID, time.Now())
	if err != nil {
		if strings.Contains(err.Error(), "duplicate key") {
			http.Error(w, "Click with this ID already exists", http.StatusBadRequest)
			return
		}
		http.Error(w, "Database error", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func clicksHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	dateStr := r.URL.Query().Get("date")
	direction := r.URL.Query().Get("direction")

	if dateStr == "" || direction == "" {
		http.Error(w, "Missing required query parameters: date and direction", http.StatusBadRequest)
		return
	}

	validDirections := map[string]bool{"<": true, ">": true, "<=": true, ">=": true}
	if !validDirections[direction] {
		http.Error(w, "Invalid direction parameter", http.StatusBadRequest)
		return
	}

	date, err := time.Parse("2006-01-02", dateStr)
	if err != nil {
		http.Error(w, "Invalid date format, use YYYY-MM-DD", http.StatusBadRequest)
		return
	}

	query := fmt.Sprintf("SELECT id, timestamp FROM clicks WHERE timestamp %s $1 ORDER BY timestamp", direction)
	rows, err := db.Query(query, date)
	if err != nil {
		http.Error(w, "Database error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	var clicks []Click
	for rows.Next() {
		var click Click
		if err := rows.Scan(&click.ID, &click.Timestamp); err != nil {
			http.Error(w, "Database error", http.StatusInternalServerError)
			return
		}
		clicks = append(clicks, click)
	}

	if err := rows.Err(); err != nil {
		http.Error(w, "Database error", http.StatusInternalServerError)
		return
	}

	if len(clicks) == 0 {
		http.Error(w, "No clicks found", http.StatusNotFound)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(clicks)
}