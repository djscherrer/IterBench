package main

import (
	"context"
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
	ID        string    `json:"id"`
	Timestamp time.Time `json:"timestamp"`
}

func initDB() error {
	host := getenv("DB_HOST", "localhost")
	port := getenv("DB_PORT", "5432")
	user := getenv("DB_USER", "postgres")
	password := getenv("DB_PASSWORD", "postgres")
	dbname := getenv("DB_NAME", "testdb")

	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, dbname)

	var err error
	db, err = sql.Open("postgres", connStr)
	if err != nil {
		return err
	}

	db.SetMaxOpenConns(50)
	db.SetMaxIdleConns(25)
	db.SetConnMaxLifetime(5 * time.Minute)
	db.SetConnMaxIdleTime(2 * time.Minute)

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	for i := 0; i < 30; i++ {
		if err = db.PingContext(ctx); err == nil {
			break
		}
		time.Sleep(1 * time.Second)
	}
	if err != nil {
		return err
	}

	schema := `
	CREATE TABLE IF NOT EXISTS clicks (
		id UUID PRIMARY KEY,
		timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
	);
	CREATE INDEX IF NOT EXISTS idx_clicks_timestamp ON clicks(timestamp);
	`
	_, err = db.ExecContext(ctx, schema)
	return err
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func handleClick(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	id := uuid.New().String()
	_, err := db.ExecContext(r.Context(), "INSERT INTO clicks (id, timestamp) VALUES ($1, NOW())", id)
	if err != nil {
		http.Error(w, "invalid request", http.StatusBadRequest)
		return
	}
	w.WriteHeader(http.StatusCreated)
}

func handleClicks(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	dateStr := r.URL.Query().Get("date")
	direction := r.URL.Query().Get("direction")
	if dateStr == "" || direction == "" {
		http.Error(w, "invalid request", http.StatusBadRequest)
		return
	}
	var op string
	switch direction {
	case "<", "<=", ">", ">=":
		op = direction
	default:
		http.Error(w, "invalid request", http.StatusBadRequest)
		return
	}
	date, err := time.Parse("2006-01-02", dateStr)
	if err != nil {
		http.Error(w, "invalid request", http.StatusBadRequest)
		return
	}

	query := fmt.Sprintf("SELECT id, timestamp FROM clicks WHERE timestamp %s $1 ORDER BY timestamp", op)
	rows, err := db.QueryContext(r.Context(), query, date)
	if err != nil {
		http.Error(w, "invalid request", http.StatusBadRequest)
		return
	}
	defer rows.Close()

	clicks := make([]Click, 0, 128)
	for rows.Next() {
		var c Click
		if err := rows.Scan(&c.ID, &c.Timestamp); err != nil {
			http.Error(w, "invalid request", http.StatusBadRequest)
			return
		}
		clicks = append(clicks, c)
	}
	if err := rows.Err(); err != nil {
		http.Error(w, "invalid request", http.StatusBadRequest)
		return
	}

	if len(clicks) == 0 {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		w.Write([]byte("[]"))
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(clicks)
}

func main() {
	if err := initDB(); err != nil {
		log.Fatalf("db init failed: %v", err)
	}
	defer db.Close()

	mux := http.NewServeMux()
	mux.HandleFunc("/click", handleClick)
	mux.HandleFunc("/clicks", handleClicks)

	port := getenv("PORT", "5001")
	addr := "0.0.0.0:" + port

	srv := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	log.Printf("Listening on %s", addr)
	if err := srv.ListenAndServe(); err != nil {
		log.Fatalf("server: %v", err)
	}
}
