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
	"strings"
	"time"

	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

type Click struct {
	ID        string    `json:"id"`
	Timestamp time.Time `json:"timestamp"`
}

type App struct {
	db *sql.DB
}

func main() {
	db, err := openDBFromEnv()
	if err != nil {
		log.Fatalf("failed to connect to database: %v", err)
	}
	defer db.Close()

	if err := initializeDatabase(db); err != nil {
		log.Fatalf("failed to initialize database: %v", err)
	}

	app := &App{db: db}

	mux := http.NewServeMux()
	mux.HandleFunc("/click", app.handleClick)
	mux.HandleFunc("/clicks", app.handleClicks)

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

func openDBFromEnv() (*sql.DB, error) {
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
		db.Close()
		return nil, err
	}

	db.SetConnMaxLifetime(30 * time.Minute)
	db.SetMaxOpenConns(10)
	db.SetMaxIdleConns(10)

	return db, nil
}

func initializeDatabase(db *sql.DB) error {
	const query = `
CREATE TABLE IF NOT EXISTS clicks (
    id UUID PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clicks_timestamp ON clicks (timestamp);
`
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	_, err := db.ExecContext(ctx, query)
	return err
}

func (a *App) handleClick(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeMethodNotAllowed(w, http.MethodPost)
		return
	}

	click := Click{
		ID:        uuid.New().String(),
		Timestamp: time.Now().UTC(),
	}

	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	_, err := a.db.ExecContext(
		ctx,
		`INSERT INTO clicks (id, timestamp) VALUES ($1, $2)`,
		click.ID,
		click.Timestamp,
	)
	if err != nil {
		http.Error(w, "failed to register click", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	_ = json.NewEncoder(w).Encode(click)
}

func (a *App) handleClicks(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeMethodNotAllowed(w, http.MethodGet)
		return
	}

	dateStr := r.URL.Query().Get("date")
	direction := r.URL.Query().Get("direction")

	if dateStr == "" || direction == "" {
		http.Error(w, "missing required query parameters: date and direction", http.StatusBadRequest)
		return
	}

	if !isValidDirection(direction) {
		http.Error(w, "invalid direction; must be one of <, >, <=, >=", http.StatusBadRequest)
		return
	}

	filterDate, err := time.Parse("2006-01-02", dateStr)
	if err != nil {
		http.Error(w, "invalid date; expected format YYYY-MM-DD", http.StatusBadRequest)
		return
	}

	query := fmt.Sprintf(
		`SELECT id, timestamp FROM clicks WHERE timestamp %s $1 ORDER BY timestamp ASC`,
		direction,
	)

	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	rows, err := a.db.QueryContext(ctx, query, filterDate.UTC())
	if err != nil {
		http.Error(w, "failed to retrieve clicks", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	var clicks []Click
	for rows.Next() {
		var click Click
		if err := rows.Scan(&click.ID, &click.Timestamp); err != nil {
			http.Error(w, "failed to read clicks", http.StatusInternalServerError)
			return
		}
		clicks = append(clicks, click)
	}

	if err := rows.Err(); err != nil {
		http.Error(w, "failed to retrieve clicks", http.StatusInternalServerError)
		return
	}

	if len(clicks) == 0 {
		http.Error(w, "no clicks found", http.StatusNotFound)
		return
	}

	writeJSON(w, http.StatusOK, clicks)
}

func isValidDirection(direction string) bool {
	switch direction {
	case "<", ">", "<=", ">=":
		return true
	default:
		return false
	}
}

func writeJSON(w http.ResponseWriter, status int, value interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func writeMethodNotAllowed(w http.ResponseWriter, allowed string) {
	w.Header().Set("Allow", allowed)
	http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
}

func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		log.Printf("%s %s %s", r.Method, r.URL.Path, strings.TrimSpace(time.Since(start).String()))
	})
}