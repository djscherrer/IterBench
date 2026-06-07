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

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func initDB() {
	connStr := fmt.Sprintf(
		"host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		getEnv("DB_HOST", "localhost"),
		getEnv("DB_PORT", "5432"),
		getEnv("DB_USER", "postgres"),
		getEnv("DB_PASSWORD", "postgres"),
		getEnv("DB_NAME", "testdb"),
	)

	var err error
	db, err = sql.Open("postgres", connStr)
	if err != nil {
		log.Fatal("Failed to open database:", err)
	}

	db.SetMaxOpenConns(100)
	db.SetMaxIdleConns(50)
	db.SetConnMaxLifetime(5 * time.Minute)

	if err = db.Ping(); err != nil {
		log.Fatal("Failed to ping database:", err)
	}

	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS clicks (
			id TEXT PRIMARY KEY,
			timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
		);
		CREATE INDEX IF NOT EXISTS idx_clicks_timestamp ON clicks (timestamp);
	`)
	if err != nil {
		log.Fatal("Failed to create table:", err)
	}
}

type Click struct {
	ID        string `json:"id"`
	Timestamp string `json:"timestamp"`
}

func handleClick(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	id := uuid.New().String()
	now := time.Now().UTC()

	_, err := db.Exec("INSERT INTO clicks (id, timestamp) VALUES ($1, $2)", id, now)
	if err != nil {
		http.Error(w, "Invalid request", http.StatusBadRequest)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func handleClicks(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	dateStr := r.URL.Query().Get("date")
	direction := r.URL.Query().Get("direction")

	if dateStr == "" || direction == "" {
		http.Error(w, "Invalid request", http.StatusBadRequest)
		return
	}

	switch direction {
	case "<", ">", "<=", ">=":
	default:
		http.Error(w, "Invalid request", http.StatusBadRequest)
		return
	}

	date, err := time.Parse("2006-01-02", dateStr)
	if err != nil {
		http.Error(w, "Invalid request", http.StatusBadRequest)
		return
	}

	var queryDate time.Time
	if direction == "<" || direction == "<=" {
		if direction == "<" {
			queryDate = date
		} else {
			queryDate = date.AddDate(0, 0, 1)
		}
	} else {
		if direction == ">" {
			queryDate = date.AddDate(0, 0, 1)
		} else {
			queryDate = date
		}
	}

	var op string
	if direction == "<" || direction == "<=" {
		op = "<"
	} else {
		op = ">="
	}

	query := fmt.Sprintf("SELECT id, timestamp FROM clicks WHERE timestamp %s $1 ORDER BY timestamp", op)
	rows, err := db.Query(query, queryDate)
	if err != nil {
		http.Error(w, "Invalid request", http.StatusBadRequest)
		return
	}
	defer rows.Close()

	clicks := make([]Click, 0)
	for rows.Next() {
		var c Click
		var ts time.Time
		if err := rows.Scan(&c.ID, &ts); err != nil {
			http.Error(w, "Invalid request", http.StatusBadRequest)
			return
		}
		c.Timestamp = ts.UTC().Format(time.RFC3339)
		clicks = append(clicks, c)
	}

	if len(clicks) == 0 {
		http.Error(w, "No clicks found", http.StatusNotFound)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(clicks)
}

func main() {
	initDB()
	defer db.Close()

	port := getEnv("PORT", "5001")

	http.HandleFunc("/click", handleClick)
	http.HandleFunc("/clicks", handleClicks)

	log.Printf("Server starting on 0.0.0.0:%s", port)
	log.Fatal(http.ListenAndServe("0.0.0.0:"+port, nil))
}
