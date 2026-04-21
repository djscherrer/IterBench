package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"time"

	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

var db *sql.DB

func initDB() {
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
		dbname = "postgres"
	}

	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, dbname)

	var err error
	db, err = sql.Open("postgres", connStr)
	if err != nil {
		log.Fatalf("Failed to open database: %v", err)
	}

	err = db.Ping()
	if err != nil {
		log.Fatalf("Failed to ping database: %v", err)
	}

	createTableSQL := `
	CREATE TABLE IF NOT EXISTS clicks (
		id TEXT PRIMARY KEY,
		timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
	);`

	_, err = db.Exec(createTableSQL)
	if err != nil {
		log.Fatalf("Failed to create table: %v", err)
	}
}

func handleClick(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusBadRequest)
		return
	}

	id := uuid.New().String()
	timestamp := time.Now().UTC()

	_, err := db.Exec("INSERT INTO clicks (id, timestamp) VALUES ($1, $2)", id, timestamp)
	if err != nil {
		log.Printf("Failed to insert click: %v", err)
		w.WriteHeader(http.StatusBadRequest)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

type ClickResponse struct {
	ID        string `json:"id"`
	Timestamp string `json:"timestamp"`
}

func handleClicks(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusBadRequest)
		return
	}

	dateStr := r.URL.Query().Get("date")
	direction := r.URL.Query().Get("direction")

	if dateStr == "" || direction == "" {
		w.WriteHeader(http.StatusBadRequest)
		return
	}

	// Validate direction
	validDirections := map[string]bool{
		"<":  true,
		">":  true,
		"<=": true,
		">=": true,
	}
	if !validDirections[direction] {
		w.WriteHeader(http.StatusBadRequest)
		return
	}

	// Parse date
	parsedDate, err := time.Parse("2006-01-02", dateStr)
	if err != nil {
		w.WriteHeader(http.StatusBadRequest)
		return
	}

	// For < and <=, we compare against the start of the day
	// For > and >=, we need to think about what makes sense
	// < date means timestamp < start of that day
	// <= date means timestamp < start of next day (i.e., the entire day is included)
	// > date means timestamp >= start of next day
	// >= date means timestamp >= start of that day

	var query string
	var queryDate time.Time

	switch direction {
	case "<":
		queryDate = parsedDate
		query = "SELECT id, timestamp FROM clicks WHERE timestamp < $1 ORDER BY timestamp"
	case "<=":
		queryDate = parsedDate.AddDate(0, 0, 1)
		query = "SELECT id, timestamp FROM clicks WHERE timestamp < $1 ORDER BY timestamp"
	case ">":
		queryDate = parsedDate.AddDate(0, 0, 1)
		query = "SELECT id, timestamp FROM clicks WHERE timestamp >= $1 ORDER BY timestamp"
	case ">=":
		queryDate = parsedDate
		query = "SELECT id, timestamp FROM clicks WHERE timestamp >= $1 ORDER BY timestamp"
	}

	rows, err := db.Query(query, queryDate)
	if err != nil {
		log.Printf("Failed to query clicks: %v", err)
		w.WriteHeader(http.StatusBadRequest)
		return
	}
	defer rows.Close()

	var clicks []ClickResponse
	for rows.Next() {
		var id string
		var ts time.Time
		if err := rows.Scan(&id, &ts); err != nil {
			log.Printf("Failed to scan row: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		clicks = append(clicks, ClickResponse{
			ID:        id,
			Timestamp: ts.Format(time.RFC3339),
		})
	}

	if err := rows.Err(); err != nil {
		log.Printf("Row iteration error: %v", err)
		w.WriteHeader(http.StatusBadRequest)
		return
	}

	if len(clicks) == 0 {
		w.WriteHeader(http.StatusNotFound)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(clicks)
}

func main() {
	initDB()

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	http.HandleFunc("/click", handleClick)
	http.HandleFunc("/clicks", handleClicks)

	addr := fmt.Sprintf("0.0.0.0:%s", port)
	log.Printf("Server listening on %s", addr)
	log.Fatal(http.ListenAndServe(addr, nil))
}