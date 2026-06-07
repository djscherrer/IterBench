package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	_ "github.com/lib/pq"
	"github.com/google/uuid"
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
		log.Fatal(err)
	}
	defer db.Close()

	if err := initDB(db); err != nil {
		log.Fatal(err)
	}

	app := &App{db: db}

	mux := http.NewServeMux()
	mux.HandleFunc("/click", app.handleClick)
	mux.HandleFunc("/clicks", app.handleClicks)

	addr := "0.0.0.0:" + getEnv("PORT", "5001")
	server := &http.Server{
		Addr:              addr,
		Handler:           loggingMiddleware(mux),
		ReadHeaderTimeout: 5 * time.Second,
	}

	log.Printf("server listening on %s", addr)
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

	dsn := "host=" + host +
		" port=" + port +
		" user=" + user +
		" password=" + password +
		" dbname=" + name +
		" sslmode=disable"

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
	const query = `
CREATE TABLE IF NOT EXISTS clicks (
	id UUID PRIMARY KEY,
	timestamp TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clicks_timestamp ON clicks (timestamp);
`
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	_, err := db.ExecContext(ctx, query)
	return err
}

func (a *App) handleClick(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/click" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}

	click := Click{
		ID:        uuid.NewString(),
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

	writeJSON(w, http.StatusCreated, click)
}

func (a *App) handleClicks(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/clicks" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodGet {
		methodNotAllowed(w, http.MethodGet)
		return
	}

	dateStr := r.URL.Query().Get("date")
	direction := r.URL.Query().Get("direction")

	if dateStr == "" || direction == "" {
		http.Error(w, "missing required query parameters: date and direction", http.StatusBadRequest)
		return
	}

	if !isValidDirection(direction) {
		http.Error(w, "invalid direction, must be one of <, >, <=, >=", http.StatusBadRequest)
		return
	}

	dateValue, err := time.Parse("2006-01-02", dateStr)
	if err != nil {
		http.Error(w, "invalid date format, expected YYYY-MM-DD", http.StatusBadRequest)
		return
	}

	query := `SELECT id, timestamp FROM clicks WHERE timestamp ` + direction + ` $1 ORDER BY timestamp ASC`

	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	rows, err := a.db.QueryContext(ctx, query, dateValue.UTC())
	if err != nil {
		http.Error(w, "failed to retrieve clicks", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	var clicks []Click
	for rows.Next() {
		var c Click
		if err := rows.Scan(&c.ID, &c.Timestamp); err != nil {
			http.Error(w, "failed to read clicks", http.StatusInternalServerError)
			return
		}
		clicks = append(clicks, c)
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

func methodNotAllowed(w http.ResponseWriter, allowed string) {
	w.Header().Set("Allow", allowed)
	http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		log.Printf("%s %s %s", r.Method, r.URL.RequestURI(), strings.TrimSpace(r.RemoteAddr))
		next.ServeHTTP(w, r)
	})
}

func getEnv(key, fallback string) string {
	value := os.Getenv(key)
	if value == "" {
		return fallback
	}
	return value
}