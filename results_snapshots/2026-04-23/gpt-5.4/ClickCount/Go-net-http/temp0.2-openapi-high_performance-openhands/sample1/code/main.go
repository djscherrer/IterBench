package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

const (
	defaultPort        = "5001"
	requestBodyLimit   = 1 << 20
	startupTimeout     = 10 * time.Second
	insertTimeout      = 2 * time.Second
	selectTimeout      = 5 * time.Second
	defaultMaxOpenConn = 64
)

type app struct {
	insertStmt *sql.Stmt
	beforeStmt *sql.Stmt
	afterStmt  *sql.Stmt
}

type click struct {
	ID        string    `json:"id"`
	Timestamp time.Time `json:"timestamp"`
}

func main() {
	logger := log.New(os.Stdout, "", log.LstdFlags|log.LUTC)

	db, err := openDBFromEnv()
	if err != nil {
		logger.Fatalf("database configuration error: %v", err)
	}
	defer db.Close()

	ctx, cancel := context.WithTimeout(context.Background(), startupTimeout)
	defer cancel()

	if err := db.PingContext(ctx); err != nil {
		logger.Fatalf("database ping failed: %v", err)
	}
	if err := initSchema(ctx, db); err != nil {
		logger.Fatalf("database initialization failed: %v", err)
	}

	insertStmt, err := db.PrepareContext(ctx, `INSERT INTO clicks (id, created_at) VALUES ($1, NOW())`)
	if err != nil {
		logger.Fatalf("prepare insert statement: %v", err)
	}
	defer insertStmt.Close()

	beforeStmt, err := db.PrepareContext(ctx, `
        SELECT id, created_at
        FROM clicks
        WHERE created_at < $1
        ORDER BY created_at ASC, id ASC
    `)
	if err != nil {
		logger.Fatalf("prepare before statement: %v", err)
	}
	defer beforeStmt.Close()

	afterStmt, err := db.PrepareContext(ctx, `
        SELECT id, created_at
        FROM clicks
        WHERE created_at >= $1
        ORDER BY created_at ASC, id ASC
    `)
	if err != nil {
		logger.Fatalf("prepare after statement: %v", err)
	}
	defer afterStmt.Close()

	application := &app{
		insertStmt: insertStmt,
		beforeStmt: beforeStmt,
		afterStmt:  afterStmt,
	}

	mux := http.NewServeMux()
	mux.HandleFunc("POST /click", application.handleRegisterClick)
	mux.HandleFunc("GET /clicks", application.handleListClicks)

	port := strings.TrimSpace(os.Getenv("PORT"))
	if port == "" {
		port = defaultPort
	}

	server := &http.Server{
		Addr:              net.JoinHostPort("0.0.0.0", port),
		Handler:           mux,
		ReadTimeout:       5 * time.Second,
		ReadHeaderTimeout: 2 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	logger.Printf("listening on %s", server.Addr)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		logger.Fatalf("server failed: %v", err)
	}
}

func openDBFromEnv() (*sql.DB, error) {
	host := strings.TrimSpace(os.Getenv("DB_HOST"))
	port := strings.TrimSpace(os.Getenv("DB_PORT"))
	user := strings.TrimSpace(os.Getenv("DB_USER"))
	password := os.Getenv("DB_PASSWORD")
	name := strings.TrimSpace(os.Getenv("DB_NAME"))

	missing := make([]string, 0, 5)
	if host == "" {
		missing = append(missing, "DB_HOST")
	}
	if port == "" {
		missing = append(missing, "DB_PORT")
	}
	if user == "" {
		missing = append(missing, "DB_USER")
	}
	if name == "" {
		missing = append(missing, "DB_NAME")
	}
	if len(missing) > 0 {
		return nil, fmt.Errorf("missing environment variables: %s", strings.Join(missing, ", "))
	}

	portNumber, err := strconv.Atoi(port)
	if err != nil || portNumber <= 0 {
		return nil, fmt.Errorf("invalid DB_PORT: %q", port)
	}

	dsn := fmt.Sprintf(
		"host=%s port=%d user=%s password=%s dbname=%s sslmode=disable",
		host,
		portNumber,
		user,
		password,
		name,
	)

	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}

	maxOpen := runtime.NumCPU() * 16
	if maxOpen < defaultMaxOpenConn {
		maxOpen = defaultMaxOpenConn
	}
	db.SetMaxOpenConns(maxOpen)
	db.SetMaxIdleConns(maxOpen / 2)
	db.SetConnMaxIdleTime(5 * time.Minute)
	db.SetConnMaxLifetime(30 * time.Minute)

	return db, nil
}

func initSchema(ctx context.Context, db *sql.DB) error {
	_, err := db.ExecContext(ctx, `
        CREATE TABLE IF NOT EXISTS clicks (
            id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_clicks_created_at ON clicks (created_at);
    `)
	return err
}

func (a *app) handleRegisterClick(w http.ResponseWriter, r *http.Request) {
	defer r.Body.Close()
	limitedBody := http.MaxBytesReader(w, r.Body, requestBodyLimit)
	if _, err := io.Copy(io.Discard, limitedBody); err != nil {
		writeTextError(w, http.StatusBadRequest, "invalid request\n")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), insertTimeout)
	defer cancel()

	if _, err := a.insertStmt.ExecContext(ctx, uuid.NewString()); err != nil {
		writeTextError(w, http.StatusInternalServerError, "internal server error\n")
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func (a *app) handleListClicks(w http.ResponseWriter, r *http.Request) {
	dateValue := r.URL.Query().Get("date")
	direction := r.URL.Query().Get("direction")
	if dateValue == "" || direction == "" {
		writeTextError(w, http.StatusBadRequest, "invalid request\n")
		return
	}

	startOfDay, err := time.Parse("2006-01-02", dateValue)
	if err != nil {
		writeTextError(w, http.StatusBadRequest, "invalid request\n")
		return
	}
	nextDay := startOfDay.Add(24 * time.Hour)

	var (
		stmt     *sql.Stmt
		boundary time.Time
	)

	switch direction {
	case "<":
		stmt = a.beforeStmt
		boundary = startOfDay
	case "<=":
		stmt = a.beforeStmt
		boundary = nextDay
	case ">":
		stmt = a.afterStmt
		boundary = nextDay
	case ">=":
		stmt = a.afterStmt
		boundary = startOfDay
	default:
		writeTextError(w, http.StatusBadRequest, "invalid request\n")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), selectTimeout)
	defer cancel()

	rows, err := stmt.QueryContext(ctx, boundary)
	if err != nil {
		writeTextError(w, http.StatusInternalServerError, "internal server error\n")
		return
	}
	defer rows.Close()

	clicks := make([]click, 0, 128)
	for rows.Next() {
		var item click
		if err := rows.Scan(&item.ID, &item.Timestamp); err != nil {
			writeTextError(w, http.StatusInternalServerError, "internal server error\n")
			return
		}
		clicks = append(clicks, item)
	}
	if err := rows.Err(); err != nil {
		writeTextError(w, http.StatusInternalServerError, "internal server error\n")
		return
	}
	if len(clicks) == 0 {
		writeTextError(w, http.StatusNotFound, "no clicks found\n")
		return
	}

	w.Header().Set("Content-Type", "application/json")
	encoder := json.NewEncoder(w)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(clicks); err != nil {
		writeTextError(w, http.StatusInternalServerError, "internal server error\n")
		return
	}
}

func writeTextError(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(status)
	_, _ = io.WriteString(w, message)
}
