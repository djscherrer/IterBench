package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

type Click struct {
	ID        string    `json:"id"`
	Timestamp time.Time `json:"timestamp"`
}

type ClickResponse struct {
	ID        string `json:"id"`
	Timestamp string `json:"timestamp"`
}

type App struct {
	db *sql.DB
}

func main() {
	app := &App{}
	app.initDB()
	app.initServer()
}

func (app *App) initDB() {
	host := os.Getenv("DB_HOST")
	port := os.Getenv("DB_PORT")
	user := os.Getenv("DB_USER")
	password := os.Getenv("DB_PASSWORD")
	dbname := os.Getenv("DB_NAME")

	if host == "" {
		host = "localhost"
	}
	if port == "" {
		port = "5432"
	}
	if user == "" {
		user = "postgres"
	}
	if password == "" {
		password = "postgres"
	}
	if dbname == "" {
		dbname = "clicks"
	}

	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, dbname)

	var err error
	app.db, err = sql.Open("postgres", connStr)
	if err != nil {
		log.Fatal("Failed to connect to database:", err)
	}

	err = app.db.Ping()
	if err != nil {
		log.Fatal("Failed to ping database:", err)
	}

	_, err = app.db.Exec(`
		CREATE TABLE IF NOT EXISTS clicks (
			id UUID PRIMARY KEY,
			timestamp TIMESTAMP WITH TIME ZONE NOT NULL
		)
	`)
	if err != nil {
		log.Fatal("Failed to create table:", err)
	}
}

func (app *App) initServer() {
	http.HandleFunc("/click", app.handleClick)
	http.HandleFunc("/clicks", app.handleGetClicks)

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	addr := fmt.Sprintf("0.0.0.0:%s", port)
	log.Printf("Server starting on %s", addr)
	log.Fatal(http.ListenAndServe(addr, nil))
}

func (app *App) handleClick(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	id := uuid.New()
	timestamp := time.Now().UTC()

	_, err := app.db.Exec("INSERT INTO clicks (id, timestamp) VALUES ($1, $2)", id, timestamp)
	if err != nil {
		http.Error(w, "Failed to insert click", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func (app *App) handleGetClicks(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	dateStr := r.URL.Query().Get("date")
	direction := r.URL.Query().Get("direction")

	if dateStr == "" || direction == "" {
		http.Error(w, "Missing required parameters: date and direction", http.StatusBadRequest)
		return
	}

	date, err := time.Parse("2006-01-02", dateStr)
	if err != nil {
		http.Error(w, "Invalid date format. Use YYYY-MM-DD", http.StatusBadRequest)
		return
	}

	validDirections := map[string]bool{"<": true, ">": true, "<=": true, ">=": true}
	if !validDirections[direction] {
		http.Error(w, "Invalid direction. Use <, >, <=, or >=", http.StatusBadRequest)
		return
	}

	query := fmt.Sprintf("SELECT id, timestamp FROM clicks WHERE timestamp %s $1 ORDER BY timestamp", direction)
	rows, err := app.db.Query(query, date)
	if err != nil {
		http.Error(w, "Failed to query clicks", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	var clicks []ClickResponse
	for rows.Next() {
		var click Click
		err := rows.Scan(&click.ID, &click.Timestamp)
		if err != nil {
			http.Error(w, "Failed to scan click", http.StatusInternalServerError)
			return
		}
		clicks = append(clicks, ClickResponse{
			ID:        click.ID,
			Timestamp: click.Timestamp.Format(time.RFC3339),
		})
	}

	if err = rows.Err(); err != nil {
		http.Error(w, "Error iterating rows", http.StatusInternalServerError)
		return
	}

	if len(clicks) == 0 {
		http.Error(w, "No clicks found", http.StatusNotFound)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(clicks)
}