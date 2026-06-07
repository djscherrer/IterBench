package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/lib/pq"
)

const pageSize = 50

type app struct {
	db *sql.DB
}

type createUserRequest struct {
	Username string `json:"username"`
	FullName string `json:"full_name"`
	Bio      string `json:"bio"`
}

type createPostRequest struct {
	Username string `json:"username"`
	Content  string `json:"content"`
}

type followRequest struct {
	FollowerUsername  string `json:"follower_username"`
	FollowingUsername string `json:"following_username"`
}

type likeRequest struct {
	Username string `json:"username"`
}

type feedPost struct {
	ID        int64     `json:"id"`
	Username  string    `json:"username"`
	Content   string    `json:"content"`
	CreatedAt time.Time `json:"created_at"`
	LikeCount int       `json:"like_count"`
}

type trendingPost struct {
	ID        int64  `json:"id"`
	Username  string `json:"username"`
	Content   string `json:"content"`
	LikeCount int    `json:"like_count"`
}

type feedResponse struct {
	Items    []feedPost `json:"items"`
	Page     int        `json:"page"`
	PageSize int        `json:"page_size"`
	HasNext  bool       `json:"has_next"`
}

type trendingResponse struct {
	Items    []trendingPost `json:"items"`
	Page     int            `json:"page"`
	PageSize int            `json:"page_size"`
	HasNext  bool           `json:"has_next"`
}

func main() {
	db, err := openDB()
	if err != nil {
		log.Fatalf("database connection failed: %v", err)
	}
	defer db.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := initDB(ctx, db); err != nil {
		log.Fatalf("database initialization failed: %v", err)
	}

	a := &app{db: db}
	mux := http.NewServeMux()
	mux.HandleFunc("/users", a.handleUsers)
	mux.HandleFunc("/posts", a.handlePosts)
	mux.HandleFunc("/posts/", a.handlePostSubroutes)
	mux.HandleFunc("/follow", a.handleFollow)
	mux.HandleFunc("/feed", a.handleFeed)
	mux.HandleFunc("/trending", a.handleTrending)

	port := env("PORT", "5001")
	server := &http.Server{
		Addr:              "0.0.0.0:" + port,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	log.Printf("listening on %s", server.Addr)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("server failed: %v", err)
	}
}

func openDB() (*sql.DB, error) {
	host := env("DB_HOST", "localhost")
	port := env("DB_PORT", "5432")
	user := env("DB_USER", "postgres")
	password := os.Getenv("DB_PASSWORD")
	name := env("DB_NAME", "postgres")

	dsnURL := url.URL{
		Scheme: "postgres",
		User:   url.UserPassword(user, password),
		Host:   net.JoinHostPort(host, port),
		Path:   "/" + name,
	}
	query := dsnURL.Query()
	query.Set("sslmode", "disable")
	query.Set("connect_timeout", "5")
	dsnURL.RawQuery = query.Encode()

	db, err := sql.Open("postgres", dsnURL.String())
	if err != nil {
		return nil, err
	}

	maxOpen := runtime.GOMAXPROCS(0) * 4
	if maxOpen < 16 {
		maxOpen = 16
	}
	if maxOpen > 50 {
		maxOpen = 50
	}
	db.SetMaxOpenConns(maxOpen)
	db.SetMaxIdleConns(maxOpen)
	db.SetConnMaxLifetime(5 * time.Minute)
	db.SetConnMaxIdleTime(2 * time.Minute)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

func initDB(ctx context.Context, db *sql.DB) error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS users (
			username TEXT PRIMARY KEY,
			full_name TEXT NOT NULL,
			bio TEXT NOT NULL DEFAULT '',
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)`,
		`CREATE TABLE IF NOT EXISTS posts (
			id BIGSERIAL PRIMARY KEY,
			username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
			content TEXT NOT NULL,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			like_count INTEGER NOT NULL DEFAULT 0 CHECK (like_count >= 0)
		)`,
		`CREATE TABLE IF NOT EXISTS follows (
			follower_username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
			following_username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			PRIMARY KEY (follower_username, following_username)
		)`,
		`CREATE TABLE IF NOT EXISTS likes (
			post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
			username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			PRIMARY KEY (post_id, username)
		)`,
		`CREATE INDEX IF NOT EXISTS idx_posts_username_created_id_desc ON posts (username, created_at DESC, id DESC)`,
		`CREATE INDEX IF NOT EXISTS idx_posts_like_count_id_desc ON posts (like_count DESC, id DESC)`,
		`CREATE INDEX IF NOT EXISTS idx_follows_following_username ON follows (following_username)`,
		`CREATE INDEX IF NOT EXISTS idx_likes_username ON likes (username)`,
	}
	for _, stmt := range statements {
		if _, err := db.ExecContext(ctx, stmt); err != nil {
			return err
		}
	}
	return nil
}

func (a *app) handleUsers(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}

	var req createUserRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	username := strings.TrimSpace(req.Username)
	fullName := strings.TrimSpace(req.FullName)
	if username == "" || fullName == "" {
		writeError(w, http.StatusBadRequest, "username and full_name are required")
		return
	}

	_, err := a.db.ExecContext(r.Context(), `INSERT INTO users (username, full_name, bio) VALUES ($1, $2, $3)`, username, fullName, req.Bio)
	if err != nil {
		if isClientDBError(err) {
			writeError(w, http.StatusBadRequest, "invalid input or username already exists")
			return
		}
		writeError(w, http.StatusInternalServerError, "database error")
		return
	}
	w.WriteHeader(http.StatusCreated)
}

func (a *app) handlePosts(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/posts" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}

	var req createPostRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	username := strings.TrimSpace(req.Username)
	if username == "" || strings.TrimSpace(req.Content) == "" {
		writeError(w, http.StatusBadRequest, "username and content are required")
		return
	}

	_, err := a.db.ExecContext(r.Context(), `INSERT INTO posts (username, content) VALUES ($1, $2)`, username, req.Content)
	if err != nil {
		if isClientDBError(err) {
			writeError(w, http.StatusBadRequest, "invalid input")
			return
		}
		writeError(w, http.StatusInternalServerError, "database error")
		return
	}
	w.WriteHeader(http.StatusCreated)
}

func (a *app) handlePostSubroutes(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}
	id, matched, validID := parseLikePath(r.URL.Path)
	if !matched {
		http.NotFound(w, r)
		return
	}
	if !validID {
		writeError(w, http.StatusBadRequest, "valid postId is required")
		return
	}

	var req likeRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	username := strings.TrimSpace(req.Username)
	if username == "" || id <= 0 {
		writeError(w, http.StatusBadRequest, "valid postId and username are required")
		return
	}

	const query = `
		WITH valid AS (
			SELECT EXISTS(SELECT 1 FROM posts WHERE id = $1) AND EXISTS(SELECT 1 FROM users WHERE username = $2) AS ok
		), inserted AS (
			INSERT INTO likes (post_id, username)
			SELECT $1, $2 WHERE (SELECT ok FROM valid)
			ON CONFLICT DO NOTHING
			RETURNING 1
		), updated AS (
			UPDATE posts
			SET like_count = like_count + 1
			WHERE id = $1 AND EXISTS(SELECT 1 FROM inserted)
			RETURNING 1
		)
		SELECT (SELECT ok FROM valid), EXISTS(SELECT 1 FROM inserted), EXISTS(SELECT 1 FROM updated)`
	var valid, inserted, updated bool
	if err := a.db.QueryRowContext(r.Context(), query, id, username).Scan(&valid, &inserted, &updated); err != nil {
		if isClientDBError(err) {
			writeError(w, http.StatusBadRequest, "invalid input")
			return
		}
		writeError(w, http.StatusInternalServerError, "database error")
		return
	}
	if !valid {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}
	if inserted {
		if !updated {
			writeError(w, http.StatusInternalServerError, "database error")
			return
		}
		w.WriteHeader(http.StatusCreated)
		return
	}
	w.WriteHeader(http.StatusOK)
}

func (a *app) handleFollow(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}

	var req followRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	follower := strings.TrimSpace(req.FollowerUsername)
	following := strings.TrimSpace(req.FollowingUsername)
	if follower == "" || following == "" {
		writeError(w, http.StatusBadRequest, "follower_username and following_username are required")
		return
	}

	const query = `
		WITH valid AS (
			SELECT EXISTS(SELECT 1 FROM users WHERE username = $1) AND EXISTS(SELECT 1 FROM users WHERE username = $2) AS ok
		), inserted AS (
			INSERT INTO follows (follower_username, following_username)
			SELECT $1, $2 WHERE (SELECT ok FROM valid)
			ON CONFLICT DO NOTHING
			RETURNING 1
		)
		SELECT (SELECT ok FROM valid), EXISTS(SELECT 1 FROM inserted)`
	var valid, inserted bool
	if err := a.db.QueryRowContext(r.Context(), query, follower, following).Scan(&valid, &inserted); err != nil {
		if isClientDBError(err) {
			writeError(w, http.StatusBadRequest, "invalid input")
			return
		}
		writeError(w, http.StatusInternalServerError, "database error")
		return
	}
	if !valid {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}
	if inserted {
		w.WriteHeader(http.StatusCreated)
		return
	}
	w.WriteHeader(http.StatusOK)
}

func (a *app) handleFeed(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w)
		return
	}
	username := strings.TrimSpace(r.URL.Query().Get("username"))
	if username == "" {
		writeError(w, http.StatusBadRequest, "username is required")
		return
	}
	page, ok := parsePage(r)
	if !ok {
		writeError(w, http.StatusBadRequest, "page must be a positive integer")
		return
	}

	offset := int64(page-1) * pageSize
	rows, err := a.db.QueryContext(r.Context(), `
		SELECT p.id, p.username, p.content, p.created_at, p.like_count
		FROM posts p
		JOIN follows f ON f.following_username = p.username
		WHERE f.follower_username = $1
		ORDER BY p.created_at DESC, p.id DESC
		LIMIT $2 OFFSET $3`, username, pageSize+1, offset)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "database error")
		return
	}
	defer rows.Close()

	items := make([]feedPost, 0, pageSize+1)
	for rows.Next() {
		var item feedPost
		if err := rows.Scan(&item.ID, &item.Username, &item.Content, &item.CreatedAt, &item.LikeCount); err != nil {
			writeError(w, http.StatusInternalServerError, "database error")
			return
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		writeError(w, http.StatusInternalServerError, "database error")
		return
	}

	hasNext := len(items) > pageSize
	if hasNext {
		items = items[:pageSize]
	}
	writeJSON(w, http.StatusOK, feedResponse{Items: items, Page: page, PageSize: pageSize, HasNext: hasNext})
}

func (a *app) handleTrending(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w)
		return
	}
	page, ok := parsePage(r)
	if !ok {
		writeError(w, http.StatusBadRequest, "page must be a positive integer")
		return
	}

	offset := int64(page-1) * pageSize
	rows, err := a.db.QueryContext(r.Context(), `
		SELECT id, username, content, like_count
		FROM posts
		ORDER BY like_count DESC, id DESC
		LIMIT $1 OFFSET $2`, pageSize+1, offset)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "database error")
		return
	}
	defer rows.Close()

	items := make([]trendingPost, 0, pageSize+1)
	for rows.Next() {
		var item trendingPost
		if err := rows.Scan(&item.ID, &item.Username, &item.Content, &item.LikeCount); err != nil {
			writeError(w, http.StatusInternalServerError, "database error")
			return
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		writeError(w, http.StatusInternalServerError, "database error")
		return
	}

	hasNext := len(items) > pageSize
	if hasNext {
		items = items[:pageSize]
	}
	writeJSON(w, http.StatusOK, trendingResponse{Items: items, Page: page, PageSize: pageSize, HasNext: hasNext})
}

func parseLikePath(path string) (int64, bool, bool) {
	const prefix = "/posts/"
	const suffix = "/like"
	if !strings.HasPrefix(path, prefix) || !strings.HasSuffix(path, suffix) {
		return 0, false, false
	}
	idText := strings.TrimSuffix(strings.TrimPrefix(path, prefix), suffix)
	if strings.Contains(idText, "/") {
		return 0, false, false
	}
	if idText == "" {
		return 0, true, false
	}
	id, err := strconv.ParseInt(idText, 10, 64)
	if err != nil || id <= 0 {
		return 0, true, false
	}
	return id, true, true
}

func parsePage(r *http.Request) (int, bool) {
	pageText := r.URL.Query().Get("page")
	if pageText == "" {
		return 1, true
	}
	page64, err := strconv.ParseInt(pageText, 10, 32)
	if err != nil || page64 < 1 {
		return 0, false
	}
	return int(page64), true
}

func decodeJSON(w http.ResponseWriter, r *http.Request, dst any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
	decoder := json.NewDecoder(r.Body)
	if err := decoder.Decode(dst); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(value); err != nil {
		log.Printf("response encode failed: %v", err)
	}
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}

func methodNotAllowed(w http.ResponseWriter) {
	w.WriteHeader(http.StatusMethodNotAllowed)
}

func isClientDBError(err error) bool {
	var pqErr *pq.Error
	if errors.As(err, &pqErr) {
		switch string(pqErr.Code) {
		case "23502", "23503", "23505", "23514", "22P02":
			return true
		}
	}
	return false
}

func env(key, fallback string) string {
	value := os.Getenv(key)
	if value == "" {
		return fallback
	}
	return value
}
