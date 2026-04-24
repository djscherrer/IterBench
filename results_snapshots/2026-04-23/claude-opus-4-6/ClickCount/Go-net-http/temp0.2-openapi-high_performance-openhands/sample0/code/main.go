package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"sync"
	"time"

	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

var (
	db        *sql.DB
	batchMu   sync.Mutex
	batchBuf  []click
	batchDone chan struct{}
)

type click struct {
	ID        string `json:"id"`
	Timestamp string `json:"timestamp"`
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func initDB() {
	dsn := fmt.Sprintf(
		"host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		getEnv("DB_HOST", "localhost"),
		getEnv("DB_PORT", "5432"),
		getEnv("DB_USER", "postgres"),
		getEnv("DB_PASSWORD", "postgres"),
		getEnv("DB_NAME", "testdb"),
	)

	var err error
	db, err = sql.Open("postgres", dsn)
	if err != nil {
		panic(err)
	}

	db.SetMaxOpenConns(100)
	db.SetMaxIdleConns(50)
	db.SetConnMaxLifetime(5 * time.Minute)

	if err = db.Ping(); err != nil {
		panic(err)
	}

	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS clicks (
			id UUID PRIMARY KEY,
			timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
		);
		CREATE INDEX IF NOT EXISTS idx_clicks_timestamp ON clicks (timestamp);
	`)
	if err != nil {
		panic(err)
	}
}

func startBatchWriter() {
	batchDone = make(chan struct{})
	ticker := time.NewTicker(50 * time.Millisecond)
	go func() {
		defer close(batchDone)
		for range ticker.C {
			flushBatch()
		}
	}()
}

func flushBatch() {
	batchMu.Lock()
	if len(batchBuf) == 0 {
		batchMu.Unlock()
		return
	}
	buf := batchBuf
	batchBuf = nil
	batchMu.Unlock()

	tx, err := db.Begin()
	if err != nil {
		return
	}
	stmt, err := tx.Prepare("INSERT INTO clicks (id, timestamp) VALUES ($1, $2)")
	if err != nil {
		tx.Rollback()
		return
	}
	for _, c := range buf {
		stmt.Exec(c.ID, c.Timestamp)
	}
	stmt.Close()
	tx.Commit()
}

func handleClick(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusBadRequest)
		return
	}

	id := uuid.New().String()
	ts := time.Now().UTC().Format(time.RFC3339)

	batchMu.Lock()
	batchBuf = append(batchBuf, click{ID: id, Timestamp: ts})
	batchMu.Unlock()

	w.WriteHeader(http.StatusCreated)
}

func handleClicks(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusBadRequest)
		return
	}

	dateStr := r.URL.Query().Get("date")
	direction := r.URL.Query().Get("direction")

	if dateStr == "" || direction == "" {
		w.WriteHeader(http.StatusBadRequest)
		return
	}

	switch direction {
	case "<", ">", "<=", ">=":
	default:
		w.WriteHeader(http.StatusBadRequest)
		return
	}

	parsedDate, err := time.Parse("2006-01-02", dateStr)
	if err != nil {
		w.WriteHeader(http.StatusBadRequest)
		return
	}

	// Flush pending writes so query sees latest data
	flushBatch()

	query := fmt.Sprintf("SELECT id, timestamp FROM clicks WHERE timestamp %s $1 ORDER BY timestamp", direction)
	rows, err := db.Query(query, parsedDate)
	if err != nil {
		w.WriteHeader(http.StatusBadRequest)
		return
	}
	defer rows.Close()

	var clicks []click
	for rows.Next() {
		var c click
		var ts time.Time
		if err := rows.Scan(&c.ID, &ts); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		c.Timestamp = ts.Format(time.RFC3339)
		clicks = append(clicks, c)
	}

	if len(clicks) == 0 {
		w.WriteHeader(http.StatusNotFound)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(clicks)
}

func main() {
	initDB()
	startBatchWriter()

	mux := http.NewServeMux()
	mux.HandleFunc("/click", handleClick)
	mux.HandleFunc("/clicks", handleClicks)

	port := getEnv("PORT", "5001")
	server := &http.Server{
		Addr:         "0.0.0.0:" + port,
		Handler:      mux,
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 10 * time.Second,
		IdleTimeout:  120 * time.Second,
	}

	fmt.Printf("Server listening on 0.0.0.0:%s\n", port)
	if err := server.ListenAndServe(); err != nil {
		panic(err)
	}
}
