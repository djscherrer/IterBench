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
	host := getEnv("DB_HOST", "localhost")
	port := getEnv("DB_PORT", "5432")
	user := getEnv("DB_USER", "postgres")
	password := getEnv("DB_PASSWORD", "postgres")
	dbname := getEnv("DB_NAME", "testdb")

	dsn := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, dbname)

	var err error
	db, err = sql.Open("postgres", dsn)
	if err != nil {
		log.Fatalf("Failed to open database: %v", err)
	}
	defer db.Close()

	db.SetMaxOpenConns(100)
	db.SetMaxIdleConns(25)
	db.SetConnMaxLifetime(5 * time.Minute)

	if err := db.Ping(); err != nil {
		log.Fatalf("Failed to ping database: %v", err)
	}

	if err := initDB(); err != nil {
		log.Fatalf("Failed to initialize database: %v", err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/users", handleUsers)
	mux.HandleFunc("/posts", handlePosts)
	mux.HandleFunc("/posts/", handlePostsByID)
	mux.HandleFunc("/follow", handleFollow)
	mux.HandleFunc("/feed", handleFeed)
	mux.HandleFunc("/trending", handleTrending)

	listenPort := getEnv("PORT", "5001")
	addr := "0.0.0.0:" + listenPort
	log.Printf("Starting server on %s", addr)

	srv := &http.Server{
		Addr:         addr,
		Handler:      mux,
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 10 * time.Second,
		IdleTimeout:  60 * time.Second,
	}
	if err := srv.ListenAndServe(); err != nil {
		log.Fatalf("Server failed: %v", err)
	}
}

func initDB() error {
	schema := `
	CREATE TABLE IF NOT EXISTS users (
		id SERIAL PRIMARY KEY,
		username TEXT UNIQUE NOT NULL,
		full_name TEXT NOT NULL,
		bio TEXT DEFAULT '',
		created_at TIMESTAMPTZ DEFAULT NOW()
	);

	CREATE TABLE IF NOT EXISTS posts (
		id SERIAL PRIMARY KEY,
		username TEXT NOT NULL,
		content TEXT NOT NULL,
		like_count INTEGER DEFAULT 0,
		created_at TIMESTAMPTZ DEFAULT NOW()
	);

	CREATE TABLE IF NOT EXISTS follows (
		follower_username TEXT NOT NULL,
		following_username TEXT NOT NULL,
		created_at TIMESTAMPTZ DEFAULT NOW(),
		PRIMARY KEY (follower_username, following_username)
	);

	CREATE TABLE IF NOT EXISTS likes (
		username TEXT NOT NULL,
		post_id INTEGER NOT NULL REFERENCES posts(id),
		created_at TIMESTAMPTZ DEFAULT NOW(),
		PRIMARY KEY (username, post_id)
	);

	CREATE INDEX IF NOT EXISTS idx_posts_username_created ON posts(username, created_at DESC);
	CREATE INDEX IF NOT EXISTS idx_posts_like_id ON posts(like_count DESC, id DESC);
	CREATE INDEX IF NOT EXISTS idx_follows_follower ON follows(follower_username);
	`
	_, err := db.Exec(schema)
	return err
}

func getEnv(key, def string) string {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	return v
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
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}
	if req.Username == "" || req.FullName == "" {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	_, err := db.Exec("INSERT INTO users (username, full_name, bio) VALUES ($1, $2, $3)",
		req.Username, req.FullName, req.Bio)
	if err != nil {
		if strings.Contains(err.Error(), "duplicate") || strings.Contains(err.Error(), "unique") {
			http.Error(w, "Invalid input or username already exists", http.StatusBadRequest)
			return
		}
		http.Error(w, "Internal error", http.StatusInternalServerError)
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
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}
	if req.Username == "" || req.Content == "" {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	_, err := db.Exec("INSERT INTO posts (username, content) VALUES ($1, $2)",
		req.Username, req.Content)
	if err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}
	w.WriteHeader(http.StatusCreated)
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
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}
	if req.Username == "" {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	// Upsert-like: ignore if already liked
	_, err = db.Exec("INSERT INTO likes (username, post_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
		req.Username, postID)
	if err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	// Increment like count only if the like was actually inserted (row affected)
	// ON CONFLICT DO NOTHING returns 0 rows affected for duplicates
	// We'll just always update - but that could double-count. Let's use a different approach.
	// Actually, let's update post like_count to match actual likes count.
	_, _ = db.Exec("UPDATE posts SET like_count = (SELECT COUNT(*) FROM likes WHERE post_id = $1) WHERE id = $1", postID)

	w.WriteHeader(http.StatusCreated)
}

func handleFollow(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		FollowerUsername string `json:"follower_username"`
		FollowingUsername string `json:"following_username"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}
	if req.FollowerUsername == "" || req.FollowingUsername == "" {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	_, err := db.Exec("INSERT INTO follows (follower_username, following_username) VALUES ($1, $2) ON CONFLICT DO NOTHING",
		req.FollowerUsername, req.FollowingUsername)
	if err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}
	w.WriteHeader(http.StatusCreated)
}

func handleFeed(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	username := r.URL.Query().Get("username")
	if username == "" {
		http.Error(w, "username is required", http.StatusBadRequest)
		return
	}

	page := parseIntParam(r.URL.Query().Get("page"), 1)

	offset := (page - 1) * pageSize

	// Fetch posts from followed users, plus one extra to determine has_next
	query := `
		SELECT p.id, p.username, p.content, p.created_at, p.like_count
		FROM posts p
		JOIN follows f ON f.following_username = p.username
		WHERE f.follower_username = $1
		ORDER BY p.created_at DESC, p.id DESC
		LIMIT $2 OFFSET $3
	`
	rows, err := db.Query(query, username, pageSize+1, offset)
	if err != nil {
		http.Error(w, "Internal error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	type Post struct {
		ID        int       `json:"id"`
		Username  string    `json:"username"`
		Content   string    `json:"content"`
		CreatedAt time.Time `json:"created_at"`
		LikeCount int       `json:"like_count"`
	}

	var items []Post
	for rows.Next() {
		var p Post
		if err := rows.Scan(&p.ID, &p.Username, &p.Content, &p.CreatedAt, &p.LikeCount); err != nil {
			http.Error(w, "Internal error", http.StatusInternalServerError)
			return
		}
		items = append(items, p)
	}
	if items == nil {
		items = []Post{}
	}

	hasNext := len(items) > pageSize
	if hasNext {
		items = items[:pageSize]
	}

	resp := map[string]interface{}{
		"items":     items,
		"page":      page,
		"page_size": pageSize,
		"has_next":  hasNext,
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

func handleTrending(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	page := parseIntParam(r.URL.Query().Get("page"), 1)
	offset := (page - 1) * pageSize

	rows, err := db.Query(`
		SELECT id, username, content, like_count
		FROM posts
		ORDER BY like_count DESC, id DESC
		LIMIT $1 OFFSET $2
	`, pageSize+1, offset)
	if err != nil {
		http.Error(w, "Internal error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	type TrendingPost struct {
		ID        int    `json:"id"`
		Username  string `json:"username"`
		Content   string `json:"content"`
		LikeCount int    `json:"like_count"`
	}

	var items []TrendingPost
	for rows.Next() {
		var p TrendingPost
		if err := rows.Scan(&p.ID, &p.Username, &p.Content, &p.LikeCount); err != nil {
			http.Error(w, "Internal error", http.StatusInternalServerError)
			return
		}
		items = append(items, p)
	}
	if items == nil {
		items = []TrendingPost{}
	}

	hasNext := len(items) > pageSize
	if hasNext {
		items = items[:pageSize]
	}

	resp := map[string]interface{}{
		"items":     items,
		"page":      page,
		"page_size": pageSize,
		"has_next":  hasNext,
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

func parseIntParam(s string, def int) int {
	if s == "" {
		return def
	}
	v, err := strconv.Atoi(s)
	if err != nil || v < 1 {
		return def
	}
	return v
}
