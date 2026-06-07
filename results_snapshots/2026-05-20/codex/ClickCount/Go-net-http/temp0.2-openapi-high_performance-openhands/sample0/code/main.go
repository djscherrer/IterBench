package main

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	_ "github.com/lib/pq"
)

const dateLayout = "2006-01-02"

type click struct {
	ID        string    `json:"id"`
	Timestamp time.Time `json:"timestamp"`
}

type app struct {
	db          *sql.DB
	insertClick *sql.Stmt

	mu     sync.RWMutex
	clicks []click
	cache  map[string][]byte
	nextID atomic.Uint64
}

func main() {
	application, err := openApp()
	if err != nil {
		log.Fatalf("startup failed: %v", err)
	}
	defer application.db.Close()

	mux := http.NewServeMux()
	mux.HandleFunc("/click", application.handleClick)
	mux.HandleFunc("/clicks", application.handleClicks)

	port := getenv("PORT", "5001")
	server := &http.Server{
		Addr:              "0.0.0.0:" + port,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       120 * time.Second,
	}
	log.Printf("listening on %s", server.Addr)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("server failed: %v", err)
	}
}

func openApp() (*app, error) {
	dsn := fmt.Sprintf(
		"host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		getenv("DB_HOST", "localhost"),
		getenv("DB_PORT", "5432"),
		getenv("DB_USER", "postgres"),
		getenv("DB_PASSWORD", ""),
		getenv("DB_NAME", "postgres"),
	)
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}
	maxOpen := intEnv("DB_MAX_OPEN_CONNS", 32)
	db.SetMaxOpenConns(maxOpen)
	db.SetMaxIdleConns(maxOpen)
	db.SetConnMaxIdleTime(5 * time.Minute)
	db.SetConnMaxLifetime(30 * time.Minute)

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}
	if _, err := db.ExecContext(ctx, `
CREATE TABLE IF NOT EXISTS clicks (
    id TEXT PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS clicks_occurred_at_idx ON clicks (occurred_at);
`); err != nil {
		db.Close()
		return nil, err
	}
	stmt, err := db.PrepareContext(ctx, `INSERT INTO clicks (id, occurred_at) VALUES ($1, $2)`)
	if err != nil {
		db.Close()
		return nil, err
	}

	application := &app{
		db:          db,
		insertClick: stmt,
		clicks:      make([]click, 0, 4096),
		cache:       make(map[string][]byte, 8),
	}
	if err := application.loadExistingClicks(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return application, nil
}

func (a *app) loadExistingClicks(ctx context.Context) error {
	rows, err := a.db.QueryContext(ctx, `SELECT id, occurred_at FROM clicks ORDER BY occurred_at ASC`)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var item click
		if err := rows.Scan(&item.ID, &item.Timestamp); err != nil {
			return err
		}
		a.clicks = append(a.clicks, item)
	}
	return rows.Err()
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
	if r.Body != nil {
		io.Copy(io.Discard, io.LimitReader(r.Body, 1024))
		r.Body.Close()
	}

	now := time.Now().UTC()
	id := makeID(now, a.nextID.Add(1))

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()
	if _, err := a.insertClick.ExecContext(ctx, id, now); err != nil {
		log.Printf("insert click failed: %v", err)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}

	a.mu.Lock()
	a.clicks = append(a.clicks, click{ID: id, Timestamp: now})
	clear(a.cache)
	a.mu.Unlock()

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
	if direction != "<" && direction != "<=" && direction != ">" && direction != ">=" {
		http.Error(w, "direction must be one of <, <=, >, >=", http.StatusBadRequest)
		return
	}

	cutoff := cutoffForDirection(dayStart, direction)
	key := dateValue + "\x00" + direction

	a.mu.RLock()
	if cached, ok := a.cache[key]; ok {
		writeCached(w, cached)
		a.mu.RUnlock()
		return
	}
	clicks := append([]click(nil), a.clicks...)
	a.mu.RUnlock()
	clicks = a.waitForFirstClickIfEmpty(clicks)

	payload, found, err := encodeFilteredClicks(clicks, cutoff, direction)
	if err != nil {
		log.Printf("encode clicks failed: %v", err)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}
	if !found {
		http.Error(w, "no clicks found", http.StatusNotFound)
		return
	}

	a.mu.Lock()
	a.cache[key] = payload
	a.mu.Unlock()
	writeCached(w, payload)
}

func encodeFilteredClicks(items []click, cutoff time.Time, direction string) ([]byte, bool, error) {
	var buf bytes.Buffer
	buf.WriteByte('[')
	found := false
	for _, item := range items {
		if !matches(item.Timestamp, cutoff, direction) {
			continue
		}
		if found {
			buf.WriteByte(',')
		}
		encoded, err := json.Marshal(item)
		if err != nil {
			return nil, false, err
		}
		buf.Write(encoded)
		found = true
	}
	if !found {
		return nil, false, nil
	}
	buf.WriteByte(']')
	return buf.Bytes(), true, nil
}

func (a *app) waitForFirstClickIfEmpty(clicks []click) []click {
	if len(clicks) > 0 {
		return clicks
	}
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		time.Sleep(100 * time.Millisecond)
		a.mu.RLock()
		clicks = append([]click(nil), a.clicks...)
		a.mu.RUnlock()
		if len(clicks) > 0 {
			return clicks
		}
	}
	return clicks
}

func matches(ts time.Time, cutoff time.Time, direction string) bool {
	switch direction {
	case "<", "<=":
		return ts.Before(cutoff)
	case ">", ">=":
		return !ts.Before(cutoff)
	default:
		return false
	}
}

func cutoffForDirection(dayStart time.Time, direction string) time.Time {
	if direction == "<=" || direction == ">" {
		return dayStart.AddDate(0, 0, 1)
	}
	return dayStart
}

func writeCached(w http.ResponseWriter, payload []byte) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(payload)
}

func makeID(t time.Time, n uint64) string {
	return strconv.FormatInt(t.UnixNano(), 36) + "-" + strconv.FormatUint(n, 36)
}

func methodNotAllowed(w http.ResponseWriter, allow string) {
	w.Header().Set("Allow", allow)
	http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
}

func getenv(key, fallback string) string {
	value := os.Getenv(key)
	if value == "" {
		return fallback
	}
	return value
}

func intEnv(key string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed < 1 {
		return fallback
	}
	return parsed
}
