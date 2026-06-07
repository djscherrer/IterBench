package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"strconv"
	"time"

	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

const (
	defaultHTTPPort = "5001"
	dateLayout      = "2006-01-02"
)

type app struct {
	insertClick *sql.Stmt
	queryClicks map[string]*sql.Stmt
}

type click struct {
	ID        string    `json:"id"`
	Timestamp time.Time `json:"timestamp"`
}

func main() {
	db, err := openDB()
	if err != nil {
		log.Fatalf("database connection failed: %v", err)
	}
	defer db.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	if err := initSchema(ctx, db); err != nil {
		log.Fatalf("database initialization failed: %v", err)
	}

	application, err := newApp(ctx, db)
	if err != nil {
		log.Fatalf("application initialization failed: %v", err)
	}
	defer application.close()

	port := os.Getenv("PORT")
	if port == "" {
		port = defaultHTTPPort
	}

	server := &http.Server{
		Addr:              "0.0.0.0:" + port,
		Handler:           application.routes(),
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       120 * time.Second,
		WriteTimeout:      30 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}

	log.Printf("listening on %s", server.Addr)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("server failed: %v", err)
	}
}

func openDB() (*sql.DB, error) {
	dsn, err := postgresDSN()
	if err != nil {
		return nil, err
	}

	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}

	maxOpen := envInt("DB_MAX_OPEN_CONNS", runtime.NumCPU()*8)
	if maxOpen < 16 {
		maxOpen = 16
	}
	db.SetMaxOpenConns(maxOpen)
	db.SetMaxIdleConns(maxOpen)
	db.SetConnMaxIdleTime(5 * time.Minute)
	db.SetConnMaxLifetime(30 * time.Minute)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}

	return db, nil
}

func postgresDSN() (string, error) {
	host := envOrDefault("DB_HOST", "localhost")
	port := envOrDefault("DB_PORT", "5432")
	user := envOrDefault("DB_USER", "postgres")
	password := os.Getenv("DB_PASSWORD")
	dbName := envOrDefault("DB_NAME", "postgres")

	if _, err := strconv.Atoi(port); err != nil {
		return "", fmt.Errorf("invalid DB_PORT: %w", err)
	}

	databaseURL := url.URL{
		Scheme: "postgres",
		User:   url.UserPassword(user, password),
		Host:   net.JoinHostPort(host, port),
		Path:   dbName,
	}
	query := databaseURL.Query()
	query.Set("sslmode", "disable")
	databaseURL.RawQuery = query.Encode()

	return databaseURL.String(), nil
}

func initSchema(ctx context.Context, db *sql.DB) error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS clicks (
id UUID PRIMARY KEY,
occurred_at TIMESTAMPTZ NOT NULL
)`,
		`CREATE INDEX IF NOT EXISTS clicks_occurred_at_idx ON clicks (occurred_at)`,
	}

	for _, statement := range statements {
		if _, err := db.ExecContext(ctx, statement); err != nil {
			return err
		}
	}
	return nil
}

func newApp(ctx context.Context, db *sql.DB) (*app, error) {
	insertClick, err := db.PrepareContext(ctx, `INSERT INTO clicks (id, occurred_at) VALUES ($1, $2)`)
	if err != nil {
		return nil, err
	}

	queries := map[string]string{
		"<":  `SELECT id::text, occurred_at FROM clicks WHERE occurred_at < $1 ORDER BY occurred_at ASC`,
		"<=": `SELECT id::text, occurred_at FROM clicks WHERE occurred_at < $1 ORDER BY occurred_at ASC`,
		">":  `SELECT id::text, occurred_at FROM clicks WHERE occurred_at >= $1 ORDER BY occurred_at ASC`,
		">=": `SELECT id::text, occurred_at FROM clicks WHERE occurred_at >= $1 ORDER BY occurred_at ASC`,
	}

	queryClicks := make(map[string]*sql.Stmt, len(queries))
	for direction, statement := range queries {
		stmt, err := db.PrepareContext(ctx, statement)
		if err != nil {
			insertClick.Close()
			for _, prepared := range queryClicks {
				prepared.Close()
			}
			return nil, err
		}
		queryClicks[direction] = stmt
	}

	return &app{insertClick: insertClick, queryClicks: queryClicks}, nil
}

func (a *app) close() {
	if a.insertClick != nil {
		a.insertClick.Close()
	}
	for _, stmt := range a.queryClicks {
		stmt.Close()
	}
}

func (a *app) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/click", a.handleClick)
	mux.HandleFunc("/clicks", a.handleClicks)
	return mux
}

func (a *app) handleClick(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/click" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	_, err := a.insertClick.ExecContext(ctx, uuid.NewString(), time.Now().UTC())
	if err != nil {
		log.Printf("insert click failed: %v", err)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func (a *app) handleClicks(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/clicks" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodGet {
		methodNotAllowed(w, http.MethodGet)
		return
	}

	query := r.URL.Query()
	dateValue := query.Get("date")
	direction := query.Get("direction")
	if dateValue == "" || direction == "" {
		http.Error(w, "date and direction are required", http.StatusBadRequest)
		return
	}

	dayStart, err := time.ParseInLocation(dateLayout, dateValue, time.UTC)
	if err != nil {
		http.Error(w, "date must use YYYY-MM-DD format", http.StatusBadRequest)
		return
	}

	stmt, ok := a.queryClicks[direction]
	if !ok {
		http.Error(w, "direction must be one of <, <=, >, >=", http.StatusBadRequest)
		return
	}

	cutoff := cutoffForDirection(dayStart, direction)
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()

	rows, err := stmt.QueryContext(ctx, cutoff)
	if err != nil {
		log.Printf("query clicks failed: %v", err)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	writeClicks(w, rows)
}

func cutoffForDirection(dayStart time.Time, direction string) time.Time {
	if direction == "<=" || direction == ">" {
		return dayStart.AddDate(0, 0, 1)
	}
	return dayStart
}

func writeClicks(w http.ResponseWriter, rows *sql.Rows) {
	if !rows.Next() {
		if err := rows.Err(); err != nil {
			log.Printf("read clicks failed: %v", err)
			http.Error(w, "internal server error", http.StatusInternalServerError)
			return
		}
		http.Error(w, "no clicks found", http.StatusNotFound)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	w.Write([]byte("["))

	first := true
	for {
		var item click
		if err := rows.Scan(&item.ID, &item.Timestamp); err != nil {
			log.Printf("scan click failed: %v", err)
			return
		}

		if !first {
			w.Write([]byte(","))
		}
		encoded, err := json.Marshal(item)
		if err != nil {
			log.Printf("encode click failed: %v", err)
			return
		}
		w.Write(encoded)
		first = false

		if !rows.Next() {
			break
		}
	}

	if err := rows.Err(); err != nil {
		log.Printf("read clicks failed: %v", err)
		return
	}

	w.Write([]byte("]"))
}

func methodNotAllowed(w http.ResponseWriter, allow string) {
	w.Header().Set("Allow", allow)
	http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
}

func envOrDefault(key, fallback string) string {
	value := os.Getenv(key)
	if value == "" {
		return fallback
	}
	return value
}

func envInt(key string, fallback int) int {
	value := os.Getenv(key)
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed <= 0 {
		return fallback
	}
	return parsed
}
