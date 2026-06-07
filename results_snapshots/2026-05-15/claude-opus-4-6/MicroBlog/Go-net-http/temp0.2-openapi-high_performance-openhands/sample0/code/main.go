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
	"sync"
	"time"

	_ "github.com/lib/pq"
)

var db *sql.DB

// Simple in-memory cache for trending posts
type trendingCache struct {
	mu        sync.RWMutex
	posts     []TrendingPost
	updatedAt time.Time
	ttl       time.Duration
}

var trending = &trendingCache{ttl: 2 * time.Second}

type Post struct {
	ID        int       `json:"id"`
	Username  string    `json:"username"`
	Content   string    `json:"content"`
	LikeCount int       `json:"like_count"`
	CreatedAt time.Time `json:"created_at"`
}

type TrendingPost struct {
	ID        int    `json:"id"`
	Username  string `json:"username"`
	Content   string `json:"content"`
	LikeCount int    `json:"like_count"`
}

func initDB() {
	host := getEnv("DB_HOST", "localhost")
	port := getEnv("DB_PORT", "5432")
	user := getEnv("DB_USER", "postgres")
	password := getEnv("DB_PASSWORD", "postgres")
	dbname := getEnv("DB_NAME", "testdb")

	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, dbname)

	var err error
	db, err = sql.Open("postgres", connStr)
	if err != nil {
		log.Fatal("Failed to open database:", err)
	}

	db.SetMaxOpenConns(100)
	db.SetMaxIdleConns(50)
	db.SetConnMaxLifetime(5 * time.Minute)

	for i := 0; i < 30; i++ {
		err = db.Ping()
		if err == nil {
			break
		}
		time.Sleep(time.Second)
	}
	if err != nil {
		log.Fatal("Failed to connect to database:", err)
	}

	createTables()
}

func createTables() {
	schema := `
	CREATE TABLE IF NOT EXISTS users (
		username VARCHAR(255) PRIMARY KEY,
		full_name VARCHAR(255) NOT NULL,
		bio TEXT DEFAULT ''
	);

	CREATE TABLE IF NOT EXISTS posts (
		id SERIAL PRIMARY KEY,
		username VARCHAR(255) NOT NULL REFERENCES users(username),
		content TEXT NOT NULL,
		like_count INTEGER DEFAULT 0,
		created_at TIMESTAMPTZ DEFAULT NOW()
	);

	CREATE INDEX IF NOT EXISTS idx_posts_username ON posts(username);
	CREATE INDEX IF NOT EXISTS idx_posts_like_count ON posts(like_count DESC);
	CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at DESC);

	CREATE TABLE IF NOT EXISTS follows (
		follower_username VARCHAR(255) NOT NULL REFERENCES users(username),
		following_username VARCHAR(255) NOT NULL REFERENCES users(username),
		PRIMARY KEY (follower_username, following_username)
	);

	CREATE INDEX IF NOT EXISTS idx_follows_follower ON follows(follower_username);

	CREATE TABLE IF NOT EXISTS likes (
		post_id INTEGER NOT NULL REFERENCES posts(id),
		username VARCHAR(255) NOT NULL REFERENCES users(username),
		PRIMARY KEY (post_id, username)
	);
	`
	_, err := db.Exec(schema)
	if err != nil {
		log.Fatal("Failed to create tables:", err)
	}
}

func getEnv(key, defaultVal string) string {
	if val, ok := os.LookupEnv(key); ok {
		return val
	}
	return defaultVal
}

func writeJSON(w http.ResponseWriter, status int, data interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(data)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(map[string]string{"error": msg})
}

// POST /users
func handleCreateUser(w http.ResponseWriter, r *http.Request) {
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
		writeError(w, http.StatusBadRequest, "Invalid JSON")
		return
	}

	if req.Username == "" || req.FullName == "" {
		writeError(w, http.StatusBadRequest, "username and full_name are required")
		return
	}

	_, err := db.Exec("INSERT INTO users (username, full_name, bio) VALUES ($1, $2, $3)",
		req.Username, req.FullName, req.Bio)
	if err != nil {
		if strings.Contains(err.Error(), "duplicate key") || strings.Contains(err.Error(), "unique constraint") {
			writeError(w, http.StatusBadRequest, "Username already exists")
			return
		}
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	w.WriteHeader(http.StatusCreated)
}

// POST /posts
func handleCreatePost(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		Username string `json:"username"`
		Content  string `json:"content"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid JSON")
		return
	}

	if req.Username == "" || req.Content == "" {
		writeError(w, http.StatusBadRequest, "username and content are required")
		return
	}

	_, err := db.Exec("INSERT INTO posts (username, content) VALUES ($1, $2)",
		req.Username, req.Content)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	w.WriteHeader(http.StatusCreated)
}

// POST /posts/{postId}/like
func handleLikePost(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	// Extract postId from path: /posts/{postId}/like
	parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
	if len(parts) != 3 || parts[0] != "posts" || parts[2] != "like" {
		writeError(w, http.StatusBadRequest, "Invalid path")
		return
	}

	postID, err := strconv.Atoi(parts[1])
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid post ID")
		return
	}

	var req struct {
		Username string `json:"username"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid JSON")
		return
	}

	if req.Username == "" {
		writeError(w, http.StatusBadRequest, "username is required")
		return
	}

	// Use a transaction: insert like and update count atomically
	tx, err := db.Begin()
	if err != nil {
		writeError(w, http.StatusBadRequest, "Database error")
		return
	}
	defer tx.Rollback()

	_, err = tx.Exec("INSERT INTO likes (post_id, username) VALUES ($1, $2)", postID, req.Username)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	_, err = tx.Exec("UPDATE posts SET like_count = like_count + 1 WHERE id = $1", postID)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	if err = tx.Commit(); err != nil {
		writeError(w, http.StatusBadRequest, "Database error")
		return
	}

	w.WriteHeader(http.StatusCreated)
}

// POST /follow
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
		writeError(w, http.StatusBadRequest, "Invalid JSON")
		return
	}

	if req.FollowerUsername == "" || req.FollowingUsername == "" {
		writeError(w, http.StatusBadRequest, "follower_username and following_username are required")
		return
	}

	_, err := db.Exec("INSERT INTO follows (follower_username, following_username) VALUES ($1, $2) ON CONFLICT DO NOTHING",
		req.FollowerUsername, req.FollowingUsername)
	if err != nil {
		writeError(w, http.StatusBadRequest, "Invalid input")
		return
	}

	w.WriteHeader(http.StatusCreated)
}

// GET /feed?username=...
func handleFeed(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	username := r.URL.Query().Get("username")
	if username == "" {
		writeError(w, http.StatusBadRequest, "username query parameter is required")
		return
	}

	rows, err := db.Query(`
		SELECT p.id, p.username, p.content, p.like_count, p.created_at
		FROM posts p
		WHERE p.username = $1
		   OR p.username IN (SELECT f.following_username FROM follows f WHERE f.follower_username = $1)
		ORDER BY p.created_at DESC
		LIMIT 100
	`, username)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}
	defer rows.Close()

	posts := make([]Post, 0)
	for rows.Next() {
		var p Post
		if err := rows.Scan(&p.ID, &p.Username, &p.Content, &p.LikeCount, &p.CreatedAt); err != nil {
			writeError(w, http.StatusInternalServerError, "Scan error")
			return
		}
		posts = append(posts, p)
	}

	writeJSON(w, http.StatusOK, posts)
}

// GET /trending
func handleTrending(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	// Check cache
	trending.mu.RLock()
	if time.Since(trending.updatedAt) < trending.ttl && trending.posts != nil {
		posts := trending.posts
		trending.mu.RUnlock()
		writeJSON(w, http.StatusOK, posts)
		return
	}
	trending.mu.RUnlock()

	// Cache miss - query DB
	rows, err := db.Query(`
		SELECT id, username, content, like_count
		FROM posts
		ORDER BY like_count DESC, id DESC
		LIMIT 10
	`)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}
	defer rows.Close()

	posts := make([]TrendingPost, 0)
	for rows.Next() {
		var p TrendingPost
		if err := rows.Scan(&p.ID, &p.Username, &p.Content, &p.LikeCount); err != nil {
			writeError(w, http.StatusInternalServerError, "Scan error")
			return
		}
		posts = append(posts, p)
	}

	// Update cache
	trending.mu.Lock()
	trending.posts = posts
	trending.updatedAt = time.Now()
	trending.mu.Unlock()

	writeJSON(w, http.StatusOK, posts)
}

// Router to handle /posts and /posts/{id}/like
func postsRouter(w http.ResponseWriter, r *http.Request) {
	path := strings.Trim(r.URL.Path, "/")
	parts := strings.Split(path, "/")

	if len(parts) == 1 && parts[0] == "posts" {
		handleCreatePost(w, r)
		return
	}

	if len(parts) == 3 && parts[0] == "posts" && parts[2] == "like" {
		handleLikePost(w, r)
		return
	}

	http.NotFound(w, r)
}

func main() {
	initDB()

	port := getEnv("PORT", "5001")

	mux := http.NewServeMux()
	mux.HandleFunc("/users", handleCreateUser)
	mux.HandleFunc("/posts/", postsRouter)
	mux.HandleFunc("/posts", handleCreatePost)
	mux.HandleFunc("/follow", handleFollow)
	mux.HandleFunc("/feed", handleFeed)
	mux.HandleFunc("/trending", handleTrending)

	addr := "0.0.0.0:" + port
	log.Printf("Server starting on %s", addr)

	server := &http.Server{
		Addr:         addr,
		Handler:      mux,
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 10 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	log.Fatal(server.ListenAndServe())
}
