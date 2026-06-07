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
	"runtime"
	"strings"
	"time"

	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

const (
	dateLayout         = "2006-01-02"
	defaultPort        = "5001"
	maxIdleConnMinutes = 5
	maxLifeConnMinutes = 30
)

type click struct {
	ID        string    `json:"id"`
	Timestamp time.Time `json:"timestamp"`
}

type app struct {
	db           *sql.DB
	insertClick  *sql.Stmt
	selectBefore *sql.Stmt
	selectAfter  *sql.Stmt
}

func main() {
	logger := log.New(os.Stdout, "", log.LstdFlags|log.LUTC)

	db, err := openDB()
	if err != nil {
		logger.Fatalf("open database: %v", err)
	}
	defer db.Close()

	if err := initializeDB(context.Background(), db); err != nil {
		logger.Fatalf("initialize database: %v", err)
	}

	insertStmt, err := db.PrepareContext(context.Background(), `INSERT INTO clicks (id, timestamp) VALUES ($1, $2)`)
	if err != nil {
		logger.Fatalf("prepare insert statement: %v", err)
	}
	defer insertStmt.Close()

	selectBefore, err := db.PrepareContext(context.Background(), `SELECT id, timestamp FROM clicks WHERE timestamp < $1 ORDER BY timestamp, id`)
	if err != nil {
		logger.Fatalf("prepare before query: %v", err)
	}
	defer selectBefore.Close()

	selectAfter, err := db.PrepareContext(context.Background(), `SELECT id, timestamp FROM clicks WHERE timestamp >= $1 ORDER BY timestamp, id`)
	if err != nil {
		logger.Fatalf("prepare after query: %v", err)
	}
	defer selectAfter.Close()

	application := &app{
		insertClick:  insertStmt,
		selectBefore: selectBefore,
		selectAfter:  selectAfter,
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/click", application.handleClick)
	mux.HandleFunc("/clicks", application.handleClicks)

	port := envOrDefault("PORT", defaultPort)
	server := &http.Server{
		Addr:              "0.0.0.0:" + port,
		Handler:           mux,
		ReadHeaderTimeout: 2 * time.Second,
		ReadTimeout:       5 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	logger.Printf("listening on %s", server.Addr)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		logger.Fatalf("server error: %v", err)
	}
}

func openDB() (*sql.DB, error) {
	host := os.Getenv("DB_HOST")
	port := envOrDefault("DB_PORT", "5432")
	user := os.Getenv("DB_USER")
	password := os.Getenv("DB_PASSWORD")
	name := os.Getenv("DB_NAME")

	if host == "" || user == "" || name == "" {
		return nil, fmt.Errorf("database configuration is incomplete")
	}

	dsn := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable", host, port, user, password, name)
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}

	maxOpen := runtime.GOMAXPROCS(0) * 16
	if maxOpen < 32 {
		maxOpen = 32
	}

	db.SetMaxOpenConns(maxOpen)
	db.SetMaxIdleConns(maxOpen / 2)
	db.SetConnMaxIdleTime(maxIdleConnMinutes * time.Minute)
	db.SetConnMaxLifetime(maxLifeConnMinutes * time.Minute)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}

	return db, nil
}

func initializeDB(ctx context.Context, db *sql.DB) error {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()

	const schema = `
CREATE TABLE IF NOT EXISTS clicks (
    id UUID PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clicks_timestamp ON clicks (timestamp);
`

	_, err := db.ExecContext(ctx, schema)
	return err
}

func (a *app) handleClick(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		http.Error(w, http.StatusText(http.StatusMethodNotAllowed), http.StatusMethodNotAllowed)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	if _, err := a.insertClick.ExecContext(ctx, uuid.NewString(), time.Now().UTC()); err != nil {
		http.Error(w, http.StatusText(http.StatusInternalServerError), http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func (a *app) handleClicks(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		http.Error(w, http.StatusText(http.StatusMethodNotAllowed), http.StatusMethodNotAllowed)
		return
	}

	mode, threshold, err := parseThreshold(r.URL.Query().Get("date"), r.URL.Query().Get("direction"))
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	var rows *sql.Rows
	if mode == "before" {
		rows, err = a.selectBefore.QueryContext(ctx, threshold)
	} else {
		rows, err = a.selectAfter.QueryContext(ctx, threshold)
	}
	if err != nil {
		http.Error(w, http.StatusText(http.StatusInternalServerError), http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	if !rows.Next() {
		if err := rows.Err(); err != nil {
			http.Error(w, http.StatusText(http.StatusInternalServerError), http.StatusInternalServerError)
			return
		}
		http.Error(w, http.StatusText(http.StatusNotFound), http.StatusNotFound)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte("["))

	if err := writeClickJSON(w, rows); err != nil {
		return
	}

	for rows.Next() {
		_, _ = w.Write([]byte(","))
		if err := writeClickJSON(w, rows); err != nil {
			return
		}
	}
	if err := rows.Err(); err != nil {
		return
	}

	_, _ = w.Write([]byte("]"))
}

func writeClickJSON(w http.ResponseWriter, rows *sql.Rows) error {
	var item click
	if err := rows.Scan(&item.ID, &item.Timestamp); err != nil {
		return err
	}

	payload, err := json.Marshal(item)
	if err != nil {
		return err
	}

	_, err = w.Write(payload)
	return err
}

func parseThreshold(dateValue, direction string) (string, time.Time, error) {
	dateValue = strings.TrimSpace(dateValue)
	direction = strings.TrimSpace(direction)
	if dateValue == "" || direction == "" {
		return "", time.Time{}, fmt.Errorf("date and direction are required")
	}

	parsedDate, err := time.Parse(dateLayout, dateValue)
	if err != nil {
		return "", time.Time{}, fmt.Errorf("date must use format %s", dateLayout)
	}
	parsedDate = parsedDate.UTC()
	nextDay := parsedDate.Add(24 * time.Hour)

	switch direction {
	case "<":
		return "before", parsedDate, nil
	case "<=":
		return "before", nextDay, nil
	case ">":
		return "after", nextDay, nil
	case ">=":
		return "after", parsedDate, nil
	default:
		return "", time.Time{}, fmt.Errorf("direction must be one of <, <=, >, >=")
	}
}

func envOrDefault(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}
