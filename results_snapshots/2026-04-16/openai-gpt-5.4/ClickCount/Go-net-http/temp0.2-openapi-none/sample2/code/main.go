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

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	server := &http.Server{
		Addr:              "0.0.0.0:" + port,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}

	log.Printf("server listening on %s", server.Addr)
	log.Fatal(server.ListenAndServe())
}

func openDBFromEnv() (*sql.DB, error) {
	host := os.Getenv("DB_HOST")
	port := os.Getenv("DB_PORT")
	user := os.Getenv("DB_USER")
	password := os.Getenv("DB_PASSWORD")
	name := os.Getenv("DB_NAME")

	if host == "" || port == "" || user == "" || name == "" {
		return nil, errors.New("missing required database environment variables")
	}

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
		w.Header().Set("Allow", http.MethodPost)
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
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

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	_ = json.NewEncoder(w).Encode(click)
}

func (a *App) handleClicks(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/clicks" {
		http.NotFound(w, r)
		return
	}

	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	dateStr := r.URL.Query().Get("date")
	direction := r.URL.Query().Get("direction")

	if dateStr == "" || direction == "" {
		http.Error(w, "missing required query parameters: date and direction", http.StatusBadRequest)
		return
	}

	if !isValidDirection(direction) {
		http.Error(w, "invalid direction", http.StatusBadRequest)
		return
	}

	filterDate, err := time.Parse("2006-01-02", dateStr)
	if err != nil {
		http.Error(w, "invalid date, expected format YYYY-MM-DD", http.StatusBadRequest)
		return
	}

	query := `SELECT id, timestamp FROM clicks WHERE timestamp ` + direction + ` $1 ORDER BY timestamp ASC`

	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	rows, err := a.db.QueryContext(ctx, query, filterDate.UTC())
	if err != nil {
		http.Error(w, "failed to retrieve clicks", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	clicks := make([]Click, 0)
	for rows.Next() {
		var c Click
		if err := rows.Scan(&c.ID, &c.Timestamp); err != nil {
			http.Error(w, "failed to read clicks", http.StatusInternalServerError)
			return
		}
		clicks = append(clicks, c)
	}

	if err := rows.Err(); err != nil {
		http.Error(w, "failed to read clicks", http.StatusInternalServerError)
		return
	}

	if len(clicks) == 0 {
		http.Error(w, "no clicks found", http.StatusNotFound)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(clicks)
}

func isValidDirection(direction string) bool {
	switch strings.TrimSpace(direction) {
	case "<", ">", "<=", ">=":
		return true
	default:
		return false
	}
}