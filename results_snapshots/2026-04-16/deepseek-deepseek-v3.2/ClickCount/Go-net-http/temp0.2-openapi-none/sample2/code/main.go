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

var db *sql.DB

type Click struct {
	ID        string    `json:"id"`
	Timestamp time.Time `json:"timestamp"`
}

type ClickResponse struct {
	ID        string `json:"id"`
	Timestamp string `json:"timestamp"`
}

func initDB() {
	var err error
	host := os.Getenv("DB_HOST")
	port := os.Getenv("DB_PORT")
	user := os.Getenv("DB_USER")
	password := os.Getenv("DB_PASSWORD")
	dbname := os.Getenv("DB_NAME")

	if host == "" || port == "" || user == "" || password == "" || dbname == "" {
		log.Fatal("Database environment variables are not set")
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

	createTableSQL := `
	CREATE TABLE IF NOT EXISTS clicks (
		id UUID PRIMARY KEY,
		timestamp TIMESTAMP WITH TIME ZONE NOT NULL
	);
	`

	_, err = db.Exec(createTableSQL)
	if err != nil {
		log.Fatal(err)
	}

	log.Println("Database initialized successfully")
}

func registerClickHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	id := uuid.New()
	timestamp := time.Now().UTC()

	_, err := db.Exec("INSERT INTO clicks (id, timestamp) VALUES ($1, $2)", id, timestamp)
	if err != nil {
		http.Error(w, "Failed to register click", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func getClicksHandler(w http.ResponseWriter, r *http.Request) {
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
		http.Error(w, "Invalid direction parameter. Must be one of: <, >, <=, >=", http.StatusBadRequest)
		return
	}

	date, err := time.Parse("2006-01-02", dateStr)
	if err != nil {
		http.Error(w, "Invalid date format. Use YYYY-MM-DD", http.StatusBadRequest)
		return
	}

	query := fmt.Sprintf("SELECT id, timestamp FROM clicks WHERE timestamp %s $1 ORDER BY timestamp", direction)
	rows, err := db.Query(query, date)
	if err != nil {
		http.Error(w, "Failed to query clicks", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	var clicks []ClickResponse
	found := false

	for rows.Next() {
		found = true
		var click Click
		err := rows.Scan(&click.ID, &click.Timestamp)
		if err != nil {
			http.Error(w, "Failed to read click data", http.StatusInternalServerError)
			return
		}

		clicks = append(clicks, ClickResponse{
			ID:        click.ID,
			Timestamp: click.Timestamp.Format(time.RFC3339),
		})
	}

	if err = rows.Err(); err != nil {
		http.Error(w, "Failed to process clicks", http.StatusInternalServerError)
		return
	}

	if !found {
		http.Error(w, "No clicks found", http.StatusNotFound)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(clicks)
}

func main() {
	initDB()

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	if _, err := strconv.Atoi(port); err != nil {
		log.Fatalf("Invalid PORT value: %s", port)
	}

	http.HandleFunc("/click", registerClickHandler)
	http.HandleFunc("/clicks", getClicksHandler)

	serverAddr := fmt.Sprintf("0.0.0.0:%s", port)
	log.Printf("Server starting on %s", serverAddr)
	log.Fatal(http.ListenAndServe(serverAddr, nil))
}