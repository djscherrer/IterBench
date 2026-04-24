package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"strings"
	"time"

	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

const (
	defaultPort        = "5001"
	requestBodyLimit   = 1 << 20
	dbPingTimeout      = 5 * time.Second
	dbQueryTimeout     = 3 * time.Second
	serverReadTimeout  = 5 * time.Second
	serverWriteTimeout = 10 * time.Second
	serverIdleTimeout  = 60 * time.Second
)

type app struct {
	insertClickStmt *sql.Stmt
	queryBeforeStmt *sql.Stmt
	queryAfterStmt  *sql.Stmt
}

type click struct {
	ID        string    `json:"id"`
	Timestamp time.Time `json:"timestamp"`
}

type errorResponse struct {
	Error string `json:"error"`
}

type queryPlan struct {
	stmt     *sql.Stmt
	boundary time.Time
}

func main() {
	logger := log.New(os.Stdout, "", log.LstdFlags|log.LUTC)

	db, err := openDatabaseFromEnv()
	if err != nil {
		logger.Fatalf("database setup failed: %v", err)
	}
	defer db.Close()

	if err := initializeSchema(db); err != nil {
		logger.Fatalf("database initialization failed: %v", err)
	}

	application, err := newApp(db)
	if err != nil {
		logger.Fatalf("statement preparation failed: %v", err)
	}
	defer application.close()

	port := envOrDefault("PORT", defaultPort)
	addr := "0.0.0.0:" + port

	server := &http.Server{
		Addr:              addr,
		Handler:           application.routes(),
		ReadHeaderTimeout: serverReadTimeout,
		ReadTimeout:       serverReadTimeout,
		WriteTimeout:      serverWriteTimeout,
		IdleTimeout:       serverIdleTimeout,
		MaxHeaderBytes:    1 << 20,
	}

	logger.Printf("listening on %s", addr)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		logger.Fatalf("server failed: %v", err)
	}
}

func openDatabaseFromEnv() (*sql.DB, error) {
	host, err := requiredEnv("DB_HOST")
	if err != nil {
		return nil, err
	}
	port, err := requiredEnv("DB_PORT")
	if err != nil {
		return nil, err
	}
	user, err := requiredEnv("DB_USER")
	if err != nil {
		return nil, err
	}
	password, err := requiredEnv("DB_PASSWORD")
	if err != nil {
		return nil, err
	}
	name, err := requiredEnv("DB_NAME")
	if err != nil {
		return nil, err
	}

	hosts := []string{host}
	if host != "127.0.0.1" && host != "localhost" {
		hosts = append(hosts, "127.0.0.1", "localhost")
	}

	var lastErr error
	for _, candidateHost := range hosts {
		db, err := openDatabase(candidateHost, port, user, password, name)
		if err == nil {
			return db, nil
		}
		lastErr = err
	}

	return nil, lastErr
}

func openDatabase(host, port, user, password, database string) (*sql.DB, error) {
	dsn := buildPostgresURL(host, port, user, password, database)
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}

	maxOpenConns := runtime.GOMAXPROCS(0) * 8
	if maxOpenConns < 32 {
		maxOpenConns = 32
	}
	db.SetMaxOpenConns(maxOpenConns)
	db.SetMaxIdleConns(maxOpenConns)
	db.SetConnMaxLifetime(5 * time.Minute)
	db.SetConnMaxIdleTime(90 * time.Second)

	ctx, cancel := context.WithTimeout(context.Background(), dbPingTimeout)
	defer cancel()

	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}

	return db, nil
}

func buildPostgresURL(host, port, user, password, database string) string {
	connectionURL := &url.URL{
		Scheme: "postgres",
		User:   url.UserPassword(user, password),
		Host:   net.JoinHostPort(host, port),
		Path:   database,
	}

	query := connectionURL.Query()
	query.Set("sslmode", "disable")
	query.Set("connect_timeout", "5")
	query.Set("timezone", "UTC")
	connectionURL.RawQuery = query.Encode()

	return connectionURL.String()
}

func initializeSchema(db *sql.DB) error {
	ctx, cancel := context.WithTimeout(context.Background(), dbQueryTimeout)
	defer cancel()

	const schema = `
CREATE TABLE IF NOT EXISTS clicks (
    id TEXT PRIMARY KEY,
    clicked_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clicks_clicked_at_id ON clicks (clicked_at, id);
`

	_, err := db.ExecContext(ctx, schema)
	return err
}

func newApp(db *sql.DB) (*app, error) {
	insertClickStmt, err := db.Prepare(`INSERT INTO clicks (id, clicked_at) VALUES ($1, $2)`)
	if err != nil {
		return nil, err
	}

	queryBeforeStmt, err := db.Prepare(`SELECT id, clicked_at FROM clicks WHERE clicked_at < $1 ORDER BY clicked_at ASC, id ASC`)
	if err != nil {
		insertClickStmt.Close()
		return nil, err
	}

	queryAfterStmt, err := db.Prepare(`SELECT id, clicked_at FROM clicks WHERE clicked_at >= $1 ORDER BY clicked_at ASC, id ASC`)
	if err != nil {
		queryBeforeStmt.Close()
		insertClickStmt.Close()
		return nil, err
	}

	return &app{
		insertClickStmt: insertClickStmt,
		queryBeforeStmt: queryBeforeStmt,
		queryAfterStmt:  queryAfterStmt,
	}, nil
}

func (a *app) close() {
	if a.queryAfterStmt != nil {
		a.queryAfterStmt.Close()
	}
	if a.queryBeforeStmt != nil {
		a.queryBeforeStmt.Close()
	}
	if a.insertClickStmt != nil {
		a.insertClickStmt.Close()
	}
}

func (a *app) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/click", a.handleClick)
	mux.HandleFunc("/clicks", a.handleClicks)
	return mux
}

func (a *app) handleClick(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}

	if err := discardBody(w, r); err != nil {
		writeJSON(w, http.StatusBadRequest, errorResponse{Error: "invalid request"})
		return
	}

	createdClick := click{
		ID:        uuid.NewString(),
		Timestamp: time.Now().UTC(),
	}

	ctx, cancel := context.WithTimeout(r.Context(), dbQueryTimeout)
	defer cancel()

	if _, err := a.insertClickStmt.ExecContext(ctx, createdClick.ID, createdClick.Timestamp); err != nil {
		writeJSON(w, http.StatusInternalServerError, errorResponse{Error: "failed to register click"})
		return
	}

	writeJSON(w, http.StatusCreated, createdClick)
}

func (a *app) handleClicks(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w, http.MethodGet)
		return
	}

	plan, err := buildQueryPlan(r.URL.Query().Get("date"), r.URL.Query().Get("direction"), a.queryBeforeStmt, a.queryAfterStmt)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, errorResponse{Error: "invalid request"})
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), dbQueryTimeout)
	defer cancel()

	rows, err := plan.stmt.QueryContext(ctx, plan.boundary)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, errorResponse{Error: "failed to retrieve clicks"})
		return
	}
	defer rows.Close()

	clicks := make([]click, 0, 64)
	for rows.Next() {
		var item click
		if err := rows.Scan(&item.ID, &item.Timestamp); err != nil {
			writeJSON(w, http.StatusInternalServerError, errorResponse{Error: "failed to retrieve clicks"})
			return
		}
		clicks = append(clicks, item)
	}

	if err := rows.Err(); err != nil {
		writeJSON(w, http.StatusInternalServerError, errorResponse{Error: "failed to retrieve clicks"})
		return
	}

	if len(clicks) == 0 {
		writeJSON(w, http.StatusNotFound, errorResponse{Error: "no clicks found"})
		return
	}

	writeJSON(w, http.StatusOK, clicks)
}

func buildQueryPlan(dateValue, direction string, beforeStmt, afterStmt *sql.Stmt) (queryPlan, error) {
	if dateValue == "" || direction == "" {
		return queryPlan{}, errors.New("missing query parameters")
	}

	date, err := time.Parse("2006-01-02", dateValue)
	if err != nil {
		return queryPlan{}, err
	}

	startOfDay := date.UTC()
	nextDay := startOfDay.AddDate(0, 0, 1)

	switch direction {
	case "<":
		return queryPlan{stmt: beforeStmt, boundary: startOfDay}, nil
	case "<=":
		return queryPlan{stmt: beforeStmt, boundary: nextDay}, nil
	case ">":
		return queryPlan{stmt: afterStmt, boundary: nextDay}, nil
	case ">=":
		return queryPlan{stmt: afterStmt, boundary: startOfDay}, nil
	default:
		return queryPlan{}, errors.New("invalid direction")
	}
}

func discardBody(w http.ResponseWriter, r *http.Request) error {
	if r.Body == nil {
		return nil
	}
	defer r.Body.Close()

	limitedBody := http.MaxBytesReader(w, r.Body, requestBodyLimit)
	_, err := io.Copy(io.Discard, limitedBody)
	return err
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)

	encoder := json.NewEncoder(w)
	encoder.SetEscapeHTML(false)
	_ = encoder.Encode(payload)
}

func methodNotAllowed(w http.ResponseWriter, allowedMethods ...string) {
	w.Header().Set("Allow", strings.Join(allowedMethods, ", "))
	writeJSON(w, http.StatusMethodNotAllowed, errorResponse{Error: "method not allowed"})
}

func envOrDefault(key, fallback string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	return value
}

func requiredEnv(key string) (string, error) {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return "", fmt.Errorf("%s is required", key)
	}
	return value, nil
}
