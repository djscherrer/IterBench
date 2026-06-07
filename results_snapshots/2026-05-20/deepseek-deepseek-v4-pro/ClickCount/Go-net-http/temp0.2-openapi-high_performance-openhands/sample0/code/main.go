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

func main() {
	initDB()
	defer db.Close()

	http.HandleFunc("/click", handleClick)
	http.HandleFunc("/clicks", handleClicks)

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	log.Printf("Listening on 0.0.0.0:%s", port)
	log.Fatal(http.ListenAndServe("0.0.0.0:"+port, nil))
}

func initDB() {
	host := getEnv("DB_HOST", "localhost")
	dbPort := getEnv("DB_PORT", "5432")
	user := getEnv("DB_USER", "postgres")
	password := getEnv("DB_PASSWORD", "postgres")
	dbname := getEnv("DB_NAME", "testdb")

	connStr := fmt.Sprintf(
		"host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, dbPort, user, password, dbname,
	)

	var err error
	db, err = sql.Open("postgres", connStr)
	if err != nil {
		log.Fatalf("Failed to open DB: %v", err)
	}

	db.SetMaxOpenConns(50)
	db.SetMaxIdleConns(25)
	db.SetConnMaxLifetime(5 * time.Minute)

	if err = db.Ping(); err != nil {
		log.Fatalf("Failed to ping DB: %v", err)
	}

	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS clicks (
			id TEXT PRIMARY KEY,
			timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)
	`)
	if err != nil {
		log.Fatalf("Failed to create table: %v", err)
	}

	_, err = db.Exec(`CREATE INDEX IF NOT EXISTS idx_clicks_timestamp ON clicks (timestamp)`)
	if err != nil {
		log.Fatalf("Failed to create index: %v", err)
	}

	log.Println("Database initialized")
}

func handleClick(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	id := uuid.New().String()
	now := time.Now().UTC()

	_, err := db.Exec(`INSERT INTO clicks (id, timestamp) VALUES ($1, $2)`, id, now)
	if err != nil {
		log.Printf("Insert error: %v", err)
		http.Error(w, `{"error":"Internal server error"}`, http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(map[string]string{
		"id":        id,
		"timestamp": now.Format(time.RFC3339Nano),
	})
}

func handleClicks(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	dateStr := r.URL.Query().Get("date")
	direction := r.URL.Query().Get("direction")

	if dateStr == "" || direction == "" {
		http.Error(w, `{"error":"date and direction query parameters are required"}`, http.StatusBadRequest)
		return
	}

	validDirections := map[string]bool{"<": true, ">": true, "<=": true, ">=": true}
	if !validDirections[direction] {
		http.Error(w, `{"error":"direction must be one of: <, >, <=, >="}`, http.StatusBadRequest)
		return
	}

	query := fmt.Sprintf("SELECT id, timestamp FROM clicks WHERE timestamp %s $1 ORDER BY timestamp ASC", direction)

	rows, err := db.Query(query, dateStr)
	if err != nil {
		log.Printf("Query error: %v", err)
		http.Error(w, `{"error":"Internal server error"}`, http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	type Click struct {
		ID        string `json:"id"`
		Timestamp string `json:"timestamp"`
	}

	clicks := make([]Click, 0)
	for rows.Next() {
		var c Click
		var ts time.Time
		if err := rows.Scan(&c.ID, &ts); err != nil {
			log.Printf("Scan error: %v", err)
			continue
		}
		c.Timestamp = ts.Format(time.RFC3339Nano)
		clicks = append(clicks, c)
	}

	if err := rows.Err(); err != nil {
		log.Printf("Rows iteration error: %v", err)
		http.Error(w, `{"error":"Internal server error"}`, http.StatusInternalServerError)
		return
	}

	if len(clicks) == 0 {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		json.NewEncoder(w).Encode([]interface{}{})
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(clicks)
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
