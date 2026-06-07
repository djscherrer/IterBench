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

type Click struct {
	ID        string `json:"id"`
	Timestamp string `json:"timestamp"`
}

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
		dbname = "testdb"
	}

	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, dbname)

	var err error
	db, err = sql.Open("postgres", connStr)
	if err != nil {
		log.Fatal("Failed to open database:", err)
	}

	db.SetMaxOpenConns(100)
	db.SetMaxIdleConns(50)
	db.SetConnMaxLifetime(5 * time.Minute)

	err = db.Ping()
	if err != nil {
		log.Fatal("Failed to connect to database:", err)
	}

	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS clicks (
			id UUID PRIMARY KEY,
			timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
		);
		CREATE INDEX IF NOT EXISTS idx_clicks_timestamp ON clicks (timestamp);
	`)
	if err != nil {
		log.Fatal("Failed to create table:", err)
	}
}

func handleClick(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusBadRequest)
		return
	}

	id := uuid.New().String()
	ts := time.Now().UTC()

	_, err := db.Exec("INSERT INTO clicks (id, timestamp) VALUES ($1, $2)", id, ts)
	if err != nil {
		w.WriteHeader(http.StatusBadRequest)
		return
	}

	w.WriteHeader(http.StatusCreated)
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

	_, err := time.Parse("2006-01-02", dateStr)
	if err != nil {
		w.WriteHeader(http.StatusBadRequest)
		return
	}

	var op string
	switch direction {
	case "<":
		op = "<"
	case ">":
		op = ">"
	case "<=":
		op = "<="
	case ">=":
		op = ">="
	default:
		w.WriteHeader(http.StatusBadRequest)
		return
	}

	query := fmt.Sprintf("SELECT id, timestamp FROM clicks WHERE timestamp %s $1 ORDER BY timestamp", op)
	rows, err := db.Query(query, dateStr)
	if err != nil {
		w.WriteHeader(http.StatusBadRequest)
		return
	}
	defer rows.Close()

	clicks := make([]Click, 0)
	for rows.Next() {
		var c Click
		var ts time.Time
		if err := rows.Scan(&c.ID, &ts); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		c.Timestamp = ts.Format(time.RFC3339)
		clicks = append(clicks, c)
	}

	if err := rows.Err(); err != nil {
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
	defer db.Close()

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/click", handleClick)
	mux.HandleFunc("/clicks", handleClicks)

	addr := "0.0.0.0:" + port
	log.Printf("Server starting on %s", addr)
	log.Fatal(http.ListenAndServe(addr, mux))
}
