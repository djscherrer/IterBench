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

const pageSize = 50

var db *sql.DB

type createUserReq struct {
	Username string `json:"username"`
	FullName string `json:"full_name"`
	Bio      string `json:"bio"`
}

type createPostReq struct {
	Username string `json:"username"`
	Content  string `json:"content"`
}

type followReq struct {
	Follower  string `json:"follower_username"`
	Following string `json:"following_username"`
}

type likeReq struct {
	Username string `json:"username"`
}

type feedItem struct {
	ID        int64     `json:"id"`
	Username  string    `json:"username"`
	Content   string    `json:"content"`
	CreatedAt time.Time `json:"created_at"`
	LikeCount int64     `json:"like_count"`
}

type trendingItem struct {
	ID        int64  `json:"id"`
	Username  string `json:"username"`
	Content   string `json:"content"`
	LikeCount int64  `json:"like_count"`
}

type pageResp struct {
	Items    interface{} `json:"items"`
	Page     int         `json:"page"`
	PageSize int         `json:"page_size"`
	HasNext  bool        `json:"has_next"`
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if v != nil {
		_ = json.NewEncoder(w).Encode(v)
	}
}

func initSchema() error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS users (
			id BIGSERIAL PRIMARY KEY,
			username TEXT UNIQUE NOT NULL,
			full_name TEXT NOT NULL,
			bio TEXT NOT NULL DEFAULT ''
		)`,
		`CREATE TABLE IF NOT EXISTS posts (
			id BIGSERIAL PRIMARY KEY,
			user_id BIGINT NOT NULL REFERENCES users(id),
			content TEXT NOT NULL,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			like_count BIGINT NOT NULL DEFAULT 0
		)`,
		`CREATE INDEX IF NOT EXISTS posts_user_id_created_at_idx ON posts(user_id, created_at DESC, id DESC)`,
		`CREATE INDEX IF NOT EXISTS posts_like_count_idx ON posts(like_count DESC, id DESC)`,
		`CREATE TABLE IF NOT EXISTS follows (
			follower_id BIGINT NOT NULL REFERENCES users(id),
			following_id BIGINT NOT NULL REFERENCES users(id),
			PRIMARY KEY (follower_id, following_id)
		)`,
		`CREATE INDEX IF NOT EXISTS follows_following_idx ON follows(following_id)`,
		`CREATE TABLE IF NOT EXISTS likes (
			user_id BIGINT NOT NULL REFERENCES users(id),
			post_id BIGINT NOT NULL REFERENCES posts(id),
			PRIMARY KEY (user_id, post_id)
		)`,
	}
	for _, s := range stmts {
		if _, err := db.Exec(s); err != nil {
			return err
		}
	}
	return nil
}

func handleCreateUser(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req createUserReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid json"})
		return
	}
	if req.Username == "" || req.FullName == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "username and full_name required"})
		return
	}
	_, err := db.Exec(`INSERT INTO users(username, full_name, bio) VALUES($1,$2,$3)`,
		req.Username, req.FullName, req.Bio)
	if err != nil {
		if strings.Contains(err.Error(), "duplicate") || strings.Contains(err.Error(), "unique") {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "username already exists"})
			return
		}
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusCreated, map[string]string{"status": "created"})
}

func handleCreatePost(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req createPostReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid json"})
		return
	}
	if req.Username == "" || req.Content == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "username and content required"})
		return
	}
	var userID int64
	err := db.QueryRow(`SELECT id FROM users WHERE username=$1`, req.Username).Scan(&userID)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "user not found"})
		return
	}
	var postID int64
	err = db.QueryRow(`INSERT INTO posts(user_id, content) VALUES($1,$2) RETURNING id`,
		userID, req.Content).Scan(&postID)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusCreated, map[string]int64{"id": postID})
}

func handleFollow(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req followReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid json"})
		return
	}
	if req.Follower == "" || req.Following == "" || req.Follower == req.Following {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid usernames"})
		return
	}
	var followerID, followingID int64
	if err := db.QueryRow(`SELECT id FROM users WHERE username=$1`, req.Follower).Scan(&followerID); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "follower not found"})
		return
	}
	if err := db.QueryRow(`SELECT id FROM users WHERE username=$1`, req.Following).Scan(&followingID); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "following not found"})
		return
	}
	res, err := db.Exec(`INSERT INTO follows(follower_id, following_id) VALUES($1,$2) ON CONFLICT DO NOTHING`,
		followerID, followingID)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	rows, _ := res.RowsAffected()
	if rows == 0 {
		writeJSON(w, http.StatusOK, map[string]string{"status": "already following"})
		return
	}
	writeJSON(w, http.StatusCreated, map[string]string{"status": "followed"})
}

func handleLike(w http.ResponseWriter, r *http.Request, postID int64) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req likeReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid json"})
		return
	}
	if req.Username == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "username required"})
		return
	}
	var userID int64
	if err := db.QueryRow(`SELECT id FROM users WHERE username=$1`, req.Username).Scan(&userID); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "user not found"})
		return
	}
	// Ensure post exists
	var exists bool
	if err := db.QueryRow(`SELECT EXISTS(SELECT 1 FROM posts WHERE id=$1)`, postID).Scan(&exists); err != nil || !exists {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "post not found"})
		return
	}

	tx, err := db.Begin()
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	defer tx.Rollback()
	res, err := tx.Exec(`INSERT INTO likes(user_id, post_id) VALUES($1,$2) ON CONFLICT DO NOTHING`, userID, postID)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	rows, _ := res.RowsAffected()
	if rows == 1 {
		if _, err := tx.Exec(`UPDATE posts SET like_count = like_count + 1 WHERE id=$1`, postID); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
		if err := tx.Commit(); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, http.StatusCreated, map[string]string{"status": "liked"})
		return
	}
	if err := tx.Commit(); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "already liked"})
}

func parsePage(r *http.Request) int {
	p := r.URL.Query().Get("page")
	if p == "" {
		return 1
	}
	n, err := strconv.Atoi(p)
	if err != nil || n < 1 {
		return 1
	}
	return n
}

func handleFeed(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	username := r.URL.Query().Get("username")
	if username == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "username required"})
		return
	}
	page := parsePage(r)
	var userID int64
	if err := db.QueryRow(`SELECT id FROM users WHERE username=$1`, username).Scan(&userID); err != nil {
		// Return empty feed if user doesn't exist
		writeJSON(w, http.StatusOK, pageResp{Items: []feedItem{}, Page: page, PageSize: pageSize, HasNext: false})
		return
	}
	offset := (page - 1) * pageSize
	limit := pageSize + 1
	rows, err := db.Query(`
		SELECT p.id, u.username, p.content, p.created_at, p.like_count
		FROM posts p
		JOIN users u ON u.id = p.user_id
		WHERE p.user_id IN (SELECT following_id FROM follows WHERE follower_id=$1)
		ORDER BY p.created_at DESC, p.id DESC
		LIMIT $2 OFFSET $3`, userID, limit, offset)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	defer rows.Close()
	items := make([]feedItem, 0, pageSize)
	for rows.Next() {
		var it feedItem
		if err := rows.Scan(&it.ID, &it.Username, &it.Content, &it.CreatedAt, &it.LikeCount); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
		items = append(items, it)
	}
	hasNext := false
	if len(items) > pageSize {
		hasNext = true
		items = items[:pageSize]
	}
	writeJSON(w, http.StatusOK, pageResp{Items: items, Page: page, PageSize: pageSize, HasNext: hasNext})
}

func handleTrending(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	page := parsePage(r)
	offset := (page - 1) * pageSize
	limit := pageSize + 1
	rows, err := db.Query(`
		SELECT p.id, u.username, p.content, p.like_count
		FROM posts p
		JOIN users u ON u.id = p.user_id
		ORDER BY p.like_count DESC, p.id DESC
		LIMIT $1 OFFSET $2`, limit, offset)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	defer rows.Close()
	items := make([]trendingItem, 0, pageSize)
	for rows.Next() {
		var it trendingItem
		if err := rows.Scan(&it.ID, &it.Username, &it.Content, &it.LikeCount); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
		items = append(items, it)
	}
	hasNext := false
	if len(items) > pageSize {
		hasNext = true
		items = items[:pageSize]
	}
	writeJSON(w, http.StatusOK, pageResp{Items: items, Page: page, PageSize: pageSize, HasNext: hasNext})
}

func handlePostsRoute(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Path
	if path == "/posts" {
		handleCreatePost(w, r)
		return
	}
	// /posts/{postId}/like
	if strings.HasPrefix(path, "/posts/") {
		rest := strings.TrimPrefix(path, "/posts/")
		parts := strings.Split(rest, "/")
		if len(parts) == 2 && parts[1] == "like" {
			postID, err := strconv.ParseInt(parts[0], 10, 64)
			if err != nil {
				writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid postId"})
				return
			}
			handleLike(w, r, postID)
			return
		}
	}
	http.NotFound(w, r)
}

func main() {
	host := os.Getenv("DB_HOST")
	port := os.Getenv("DB_PORT")
	user := os.Getenv("DB_USER")
	password := os.Getenv("DB_PASSWORD")
	name := os.Getenv("DB_NAME")

	dsn := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, name)

	var err error
	db, err = sql.Open("postgres", dsn)
	if err != nil {
		log.Fatalf("failed to open db: %v", err)
	}
	db.SetMaxOpenConns(50)
	db.SetMaxIdleConns(25)
	db.SetConnMaxLifetime(5 * time.Minute)

	// Wait for DB to be ready
	for i := 0; i < 30; i++ {
		if err = db.Ping(); err == nil {
			break
		}
		time.Sleep(500 * time.Millisecond)
	}
	if err != nil {
		log.Fatalf("failed to ping db: %v", err)
	}

	if err := initSchema(); err != nil {
		log.Fatalf("failed to init schema: %v", err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/users", handleCreateUser)
	mux.HandleFunc("/follow", handleFollow)
	mux.HandleFunc("/feed", handleFeed)
	mux.HandleFunc("/trending", handleTrending)
	mux.HandleFunc("/posts", handlePostsRoute)
	mux.HandleFunc("/posts/", handlePostsRoute)

	listenPort := os.Getenv("PORT")
	if listenPort == "" {
		listenPort = "5001"
	}
	srv := &http.Server{
		Addr:         "0.0.0.0:" + listenPort,
		Handler:      mux,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 15 * time.Second,
		IdleTimeout:  60 * time.Second,
	}
	log.Printf("Listening on 0.0.0.0:%s", listenPort)
	if err := srv.ListenAndServe(); err != nil {
		log.Fatalf("server error: %v", err)
	}
}
