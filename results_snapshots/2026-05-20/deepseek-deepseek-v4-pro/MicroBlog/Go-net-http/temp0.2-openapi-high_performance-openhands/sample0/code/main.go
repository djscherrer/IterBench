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

	_ "github.com/lib/pq"
)

var db *sql.DB

const pageSize = 50

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	dbHost := os.Getenv("DB_HOST")
	dbPort := os.Getenv("DB_PORT")
	dbUser := os.Getenv("DB_USER")
	dbPassword := os.Getenv("DB_PASSWORD")
	dbName := os.Getenv("DB_NAME")

	dsn := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		dbHost, dbPort, dbUser, dbPassword, dbName)

	var err error
	db, err = sql.Open("postgres", dsn)
	if err != nil {
		log.Fatalf("Failed to open database: %v", err)
	}
	defer db.Close()

	db.SetMaxOpenConns(100)
	db.SetMaxIdleConns(25)
	db.SetConnMaxLifetime(5 * time.Minute)

	if err = db.Ping(); err != nil {
		log.Fatalf("Failed to ping database: %v", err)
	}

	if err = initDB(); err != nil {
		log.Fatalf("Failed to initialize database: %v", err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/users", handleUsers)
	mux.HandleFunc("/posts", handlePosts)
	mux.HandleFunc("/posts/", handlePostsByID)
	mux.HandleFunc("/follow", handleFollow)
	mux.HandleFunc("/feed", handleFeed)
	mux.HandleFunc("/trending", handleTrending)

	srv := &http.Server{
		Addr:         "0.0.0.0:" + port,
		Handler:      mux,
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 10 * time.Second,
		IdleTimeout:  120 * time.Second,
	}

	log.Printf("Starting server on 0.0.0.0:%s", port)
	if err := srv.ListenAndServe(); err != nil {
		log.Fatalf("Server failed: %v", err)
	}
}

func initDB() error {
	schema := `
	CREATE TABLE IF NOT EXISTS users (
		username TEXT PRIMARY KEY,
		full_name TEXT NOT NULL,
		bio TEXT DEFAULT '',
		created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
	);

	CREATE TABLE IF NOT EXISTS posts (
		id SERIAL PRIMARY KEY,
		username TEXT NOT NULL REFERENCES users(username),
		content TEXT NOT NULL,
		created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
		like_count INTEGER NOT NULL DEFAULT 0
	);

	CREATE INDEX IF NOT EXISTS idx_posts_username_created ON posts(username, created_at DESC, id DESC);
	CREATE INDEX IF NOT EXISTS idx_posts_likes ON posts(like_count DESC, id DESC);

	CREATE TABLE IF NOT EXISTS follows (
		follower_username TEXT NOT NULL REFERENCES users(username),
		following_username TEXT NOT NULL REFERENCES users(username),
		created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
		PRIMARY KEY (follower_username, following_username)
	);

	CREATE TABLE IF NOT EXISTS likes (
		username TEXT NOT NULL REFERENCES users(username),
		post_id INTEGER NOT NULL REFERENCES posts(id),
		created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
		PRIMARY KEY (username, post_id)
	);
	`
	_, err := db.Exec(schema)
	return err
}

// ---------- Handlers ----------

func handleUsers(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		Username string `json:"username"`
		FullName string `json:"full_name"`
		Bio      string `json:"bio"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}
	if req.Username == "" || req.FullName == "" {
		http.Error(w, "username and full_name are required", http.StatusBadRequest)
		return
	}

	_, err := db.Exec("INSERT INTO users (username, full_name, bio) VALUES ($1, $2, $3)",
		req.Username, req.FullName, req.Bio)
	if err != nil {
		if strings.Contains(err.Error(), "duplicate key") || strings.Contains(err.Error(), "unique") {
			http.Error(w, "Username already exists", http.StatusBadRequest)
			return
		}
		http.Error(w, "Internal server error", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func handlePosts(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		Username string `json:"username"`
		Content  string `json:"content"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}
	if req.Username == "" || req.Content == "" {
		http.Error(w, "username and content are required", http.StatusBadRequest)
		return
	}

	var id int
	var createdAt time.Time
	err := db.QueryRow(
		"INSERT INTO posts (username, content) VALUES ($1, $2) RETURNING id, created_at",
		req.Username, req.Content,
	).Scan(&id, &createdAt)
	if err != nil {
		if strings.Contains(err.Error(), "violates foreign key") {
			http.Error(w, "User does not exist", http.StatusBadRequest)
			return
		}
		http.Error(w, "Internal server error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(map[string]interface{}{
		"id":         id,
		"username":   req.Username,
		"content":    req.Content,
		"created_at": createdAt.Format(time.RFC3339Nano),
		"like_count": 0,
	})
}

func handlePostsByID(w http.ResponseWriter, r *http.Request) {
	// Path: /posts/{postId}/like
	path := strings.TrimPrefix(r.URL.Path, "/posts/")
	parts := strings.Split(path, "/")
	if len(parts) != 2 || parts[1] != "like" {
		http.Error(w, "Not found", http.StatusNotFound)
		return
	}
	postID, err := strconv.Atoi(parts[0])
	if err != nil {
		http.Error(w, "Invalid post ID", http.StatusBadRequest)
		return
	}

	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		Username string `json:"username"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}
	if req.Username == "" {
		http.Error(w, "username is required", http.StatusBadRequest)
		return
	}

	tx, err := db.Begin()
	if err != nil {
		http.Error(w, "Internal server error", http.StatusInternalServerError)
		return
	}
	defer tx.Rollback()

	result, err := tx.Exec(
		"INSERT INTO likes (username, post_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
		req.Username, postID,
	)
	if err != nil {
		if strings.Contains(err.Error(), "violates foreign key") {
			http.Error(w, "User or post does not exist", http.StatusBadRequest)
			return
		}
		http.Error(w, "Internal server error", http.StatusInternalServerError)
		return
	}

	rows, _ := result.RowsAffected()
	if rows > 0 {
		_, err = tx.Exec("UPDATE posts SET like_count = like_count + 1 WHERE id = $1", postID)
		if err != nil {
			http.Error(w, "Internal server error", http.StatusInternalServerError)
			return
		}
	}

	if err := tx.Commit(); err != nil {
		http.Error(w, "Internal server error", http.StatusInternalServerError)
		return
	}

	if rows > 0 {
		w.WriteHeader(http.StatusCreated)
	} else {
		w.WriteHeader(http.StatusOK)
	}
}

func handleFollow(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		FollowerUsername  string `json:"follower_username"`
		FollowingUsername string `json:"following_username"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}
	if req.FollowerUsername == "" || req.FollowingUsername == "" {
		http.Error(w, "follower_username and following_username are required", http.StatusBadRequest)
		return
	}

	result, err := db.Exec(
		"INSERT INTO follows (follower_username, following_username) VALUES ($1, $2) ON CONFLICT DO NOTHING",
		req.FollowerUsername, req.FollowingUsername,
	)
	if err != nil {
		if strings.Contains(err.Error(), "violates foreign key") {
			http.Error(w, "User does not exist", http.StatusBadRequest)
			return
		}
		http.Error(w, "Internal server error", http.StatusInternalServerError)
		return
	}

	rows, _ := result.RowsAffected()
	if rows > 0 {
		w.WriteHeader(http.StatusCreated)
	} else {
		w.WriteHeader(http.StatusOK)
	}
}

func handleFeed(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	username := r.URL.Query().Get("username")
	if username == "" {
		http.Error(w, "username query parameter is required", http.StatusBadRequest)
		return
	}

	page := parsePage(r.URL.Query().Get("page"))

	// Fetch 51 to determine has_next
	offset := (page - 1) * pageSize
	rows, err := db.Query(`
		SELECT p.id, p.username, p.content, p.created_at, p.like_count
		FROM posts p
		JOIN follows f ON p.username = f.following_username
		WHERE f.follower_username = $1
		ORDER BY p.created_at DESC, p.id DESC
		LIMIT $2 OFFSET $3
	`, username, pageSize+1, offset)
	if err != nil {
		http.Error(w, "Internal server error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	type feedItem struct {
		ID        int    `json:"id"`
		Username  string `json:"username"`
		Content   string `json:"content"`
		CreatedAt string `json:"created_at"`
		LikeCount int    `json:"like_count"`
	}

	items := make([]feedItem, 0, pageSize)
	for rows.Next() {
		var item feedItem
		var t time.Time
		if err := rows.Scan(&item.ID, &item.Username, &item.Content, &t, &item.LikeCount); err != nil {
			http.Error(w, "Internal server error", http.StatusInternalServerError)
			return
		}
		item.CreatedAt = t.Format(time.RFC3339Nano)
		items = append(items, item)
	}

	hasNext := len(items) > pageSize
	if hasNext {
		items = items[:pageSize]
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"items":     items,
		"page":      page,
		"page_size": pageSize,
		"has_next":  hasNext,
	})
}

func handleTrending(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	page := parsePage(r.URL.Query().Get("page"))
	offset := (page - 1) * pageSize

	rows, err := db.Query(`
		SELECT id, username, content, like_count
		FROM posts
		ORDER BY like_count DESC, id DESC
		LIMIT $1 OFFSET $2
	`, pageSize+1, offset)
	if err != nil {
		http.Error(w, "Internal server error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	type trendingItem struct {
		ID        int    `json:"id"`
		Username  string `json:"username"`
		Content   string `json:"content"`
		LikeCount int    `json:"like_count"`
	}

	items := make([]trendingItem, 0, pageSize)
	for rows.Next() {
		var item trendingItem
		if err := rows.Scan(&item.ID, &item.Username, &item.Content, &item.LikeCount); err != nil {
			http.Error(w, "Internal server error", http.StatusInternalServerError)
			return
		}
		items = append(items, item)
	}

	hasNext := len(items) > pageSize
	if hasNext {
		items = items[:pageSize]
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"items":     items,
		"page":      page,
		"page_size": pageSize,
		"has_next":  hasNext,
	})
}

// ---------- Helpers ----------

func parsePage(s string) int {
	if s == "" {
		return 1
	}
	p, err := strconv.Atoi(s)
	if err != nil || p < 1 {
		return 1
	}
	return p
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}
