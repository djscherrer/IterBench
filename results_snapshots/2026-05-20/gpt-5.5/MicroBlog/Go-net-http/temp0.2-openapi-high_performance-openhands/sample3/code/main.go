package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"

	_ "github.com/lib/pq"
)

const pageSize = 50

type app struct {
	db *sql.DB
}

type userRequest struct {
	Username string `json:"username"`
	FullName string `json:"full_name"`
	Bio      string `json:"bio"`
}

type postRequest struct {
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

type errorResponse struct {
	Error string `json:"error"`
}

func main() {
	db, err := openDB()
	if err != nil {
		log.Fatalf("database connection failed: %v", err)
	}
	defer db.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := initSchema(ctx, db); err != nil {
		log.Fatalf("database initialization failed: %v", err)
	}

	port := strings.TrimSpace(os.Getenv("PORT"))
	if port == "" {
		port = "5001"
	}

	srv := &http.Server{
		Addr:              "0.0.0.0:" + port,
		Handler:           &app{db: db},
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	log.Printf("listening on 0.0.0.0:%s", port)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("server failed: %v", err)
	}
}

func openDB() (*sql.DB, error) {
	dbURL := &url.URL{
		Scheme: "postgres",
		User:   url.UserPassword(envOrDefault("DB_USER", "postgres"), os.Getenv("DB_PASSWORD")),
		Host:   net.JoinHostPort(envOrDefault("DB_HOST", "localhost"), envOrDefault("DB_PORT", "5432")),
		Path:   envOrDefault("DB_NAME", "postgres"),
	}
	query := dbURL.Query()
	query.Set("sslmode", "disable")
	dbURL.RawQuery = query.Encode()

	db, err := sql.Open("postgres", dbURL.String())
	if err != nil {
		return nil, err
	}

	maxOpen := runtime.NumCPU() * 8
	if maxOpen < 16 {
		maxOpen = 16
	}
	if maxOpen > 100 {
		maxOpen = 100
	}
	db.SetMaxOpenConns(maxOpen)
	db.SetMaxIdleConns(maxOpen)
	db.SetConnMaxLifetime(30 * time.Minute)
	db.SetConnMaxIdleTime(10 * time.Minute)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

func envOrDefault(key, fallback string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	return value
}

func initSchema(ctx context.Context, db *sql.DB) error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS users (
			id BIGSERIAL PRIMARY KEY,
			username TEXT NOT NULL UNIQUE,
			full_name TEXT NOT NULL,
			bio TEXT NOT NULL DEFAULT '',
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)`,
		`CREATE TABLE IF NOT EXISTS posts (
			id BIGSERIAL PRIMARY KEY,
			user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
			content TEXT NOT NULL,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			like_count INTEGER NOT NULL DEFAULT 0 CHECK (like_count >= 0)
		)`,
		`CREATE TABLE IF NOT EXISTS follows (
			follower_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
			following_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			PRIMARY KEY (follower_id, following_id)
		)`,
		`CREATE TABLE IF NOT EXISTS post_likes (
			post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
			user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			PRIMARY KEY (post_id, user_id)
		)`,
		`CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)`,
		`CREATE INDEX IF NOT EXISTS idx_posts_user_created_id ON posts(user_id, created_at DESC, id DESC)`,
		`CREATE INDEX IF NOT EXISTS idx_posts_trending ON posts(like_count DESC, id DESC)`,
		`CREATE INDEX IF NOT EXISTS idx_post_likes_user ON post_likes(user_id)`,
	}

	for _, stmt := range statements {
		if _, err := db.ExecContext(ctx, stmt); err != nil {
			return err
		}
	}
	return nil
}

func (a *app) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Path
	switch {
	case path == "/users":
		if r.Method != http.MethodPost {
			methodNotAllowed(w)
			return
		}
		a.handleCreateUser(w, r)
	case path == "/posts":
		if r.Method != http.MethodPost {
			methodNotAllowed(w)
			return
		}
		a.handleCreatePost(w, r)
	case strings.HasPrefix(path, "/posts/") && strings.HasSuffix(path, "/like"):
		if r.Method != http.MethodPost {
			methodNotAllowed(w)
			return
		}
		a.handleLikePost(w, r)
	case path == "/follow":
		if r.Method != http.MethodPost {
			methodNotAllowed(w)
			return
		}
		a.handleFollow(w, r)
	case path == "/feed":
		if r.Method != http.MethodGet {
			methodNotAllowed(w)
			return
		}
		a.handleFeed(w, r)
	case path == "/trending":
		if r.Method != http.MethodGet {
			methodNotAllowed(w)
			return
		}
		a.handleTrending(w, r)
	default:
		http.NotFound(w, r)
	}
}

func (a *app) handleCreateUser(w http.ResponseWriter, r *http.Request) {
	var req userRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	req.Username = strings.TrimSpace(req.Username)
	req.FullName = strings.TrimSpace(req.FullName)
	req.Bio = strings.TrimSpace(req.Bio)
	if req.Username == "" || req.FullName == "" {
		writeError(w, http.StatusBadRequest, "username and full_name are required")
		return
	}

	ctx, cancel := requestContext(r)
	defer cancel()
	_, err := a.db.ExecContext(ctx, `INSERT INTO users(username, full_name, bio) VALUES ($1, $2, $3)`, req.Username, req.FullName, req.Bio)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid input or username already exists")
		return
	}
	writeJSON(w, http.StatusCreated, map[string]string{"status": "created"})
}

func (a *app) handleCreatePost(w http.ResponseWriter, r *http.Request) {
	var req postRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	req.Username = strings.TrimSpace(req.Username)
	req.Content = strings.TrimSpace(req.Content)
	if req.Username == "" || req.Content == "" {
		writeError(w, http.StatusBadRequest, "username and content are required")
		return
	}

	ctx, cancel := requestContext(r)
	defer cancel()
	var id int64
	err := a.db.QueryRowContext(ctx, `
		INSERT INTO posts(user_id, content)
		SELECT id, $2 FROM users WHERE username = $1
		RETURNING id`, req.Username, req.Content).Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		writeError(w, http.StatusBadRequest, "unknown username")
		return
	}
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}
	writeJSON(w, http.StatusCreated, map[string]int64{"id": id})
}

func (a *app) handleFollow(w http.ResponseWriter, r *http.Request) {
	var req followRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	req.FollowerUsername = strings.TrimSpace(req.FollowerUsername)
	req.FollowingUsername = strings.TrimSpace(req.FollowingUsername)
	if req.FollowerUsername == "" || req.FollowingUsername == "" {
		writeError(w, http.StatusBadRequest, "follower_username and following_username are required")
		return
	}

	ctx, cancel := requestContext(r)
	defer cancel()
	var followerExists, followingExists, inserted bool
	err := a.db.QueryRowContext(ctx, `
		WITH follower AS (SELECT id FROM users WHERE username = $1),
		following AS (SELECT id FROM users WHERE username = $2),
		inserted AS (
			INSERT INTO follows(follower_id, following_id)
			SELECT follower.id, following.id FROM follower, following
			ON CONFLICT DO NOTHING
			RETURNING 1
		)
		SELECT
			EXISTS (SELECT 1 FROM follower),
			EXISTS (SELECT 1 FROM following),
			EXISTS (SELECT 1 FROM inserted)`, req.FollowerUsername, req.FollowingUsername).Scan(&followerExists, &followingExists, &inserted)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}
	if !followerExists || !followingExists {
		writeError(w, http.StatusBadRequest, "unknown username")
		return
	}
	if inserted {
		writeJSON(w, http.StatusCreated, map[string]string{"status": "followed"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "already_following"})
}

func (a *app) handleLikePost(w http.ResponseWriter, r *http.Request) {
	postID, ok := parseLikePath(r.URL.Path)
	if !ok {
		http.NotFound(w, r)
		return
	}

	var req likeRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	req.Username = strings.TrimSpace(req.Username)
	if req.Username == "" {
		writeError(w, http.StatusBadRequest, "username is required")
		return
	}

	ctx, cancel := requestContext(r)
	defer cancel()
	var userExists, postExists, inserted bool
	err := a.db.QueryRowContext(ctx, `
		WITH u AS (SELECT id FROM users WHERE username = $1),
		p AS (SELECT id FROM posts WHERE id = $2),
		inserted AS (
			INSERT INTO post_likes(post_id, user_id)
			SELECT p.id, u.id FROM p, u
			ON CONFLICT DO NOTHING
			RETURNING 1
		),
		updated AS (
			UPDATE posts SET like_count = like_count + 1
			WHERE id = $2 AND EXISTS (SELECT 1 FROM inserted)
			RETURNING 1
		)
		SELECT
			EXISTS (SELECT 1 FROM u),
			EXISTS (SELECT 1 FROM p),
			EXISTS (SELECT 1 FROM inserted)`, req.Username, postID).Scan(&userExists, &postExists, &inserted)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}
	if !userExists || !postExists {
		writeError(w, http.StatusBadRequest, "unknown user or post")
		return
	}
	if inserted {
		writeJSON(w, http.StatusCreated, map[string]string{"status": "liked"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "already_liked"})
}

func (a *app) handleFeed(w http.ResponseWriter, r *http.Request) {
	username := strings.TrimSpace(r.URL.Query().Get("username"))
	if username == "" {
		writeError(w, http.StatusBadRequest, "username is required")
		return
	}
	page, ok := parsePage(r.URL.Query().Get("page"))
	if !ok {
		writeError(w, http.StatusBadRequest, "page must be a positive integer")
		return
	}
	offset := (page - 1) * pageSize

	ctx, cancel := requestContext(r)
	defer cancel()
	rows, err := a.db.QueryContext(ctx, `
		SELECT p.id, author.username, p.content, p.created_at, p.like_count
		FROM users follower
		JOIN follows f ON f.follower_id = follower.id
		JOIN posts p ON p.user_id = f.following_id
		JOIN users author ON author.id = p.user_id
		WHERE follower.username = $1
		ORDER BY p.created_at DESC, p.id DESC
		LIMIT $2 OFFSET $3`, username, pageSize+1, offset)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}
	defer rows.Close()

	items := make([]feedPost, 0, pageSize)
	for rows.Next() {
		var item feedPost
		if err := rows.Scan(&item.ID, &item.Username, &item.Content, &item.CreatedAt, &item.LikeCount); err != nil {
			writeError(w, http.StatusInternalServerError, "failed to read feed")
			return
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to read feed")
		return
	}
	hasNext := len(items) > pageSize
	if hasNext {
		items = items[:pageSize]
	}
	writeJSON(w, http.StatusOK, feedResponse{Items: items, Page: page, PageSize: pageSize, HasNext: hasNext})
}

func (a *app) handleTrending(w http.ResponseWriter, r *http.Request) {
	page, ok := parsePage(r.URL.Query().Get("page"))
	if !ok {
		writeError(w, http.StatusBadRequest, "page must be a positive integer")
		return
	}
	offset := (page - 1) * pageSize

	ctx, cancel := requestContext(r)
	defer cancel()
	rows, err := a.db.QueryContext(ctx, `
		SELECT p.id, u.username, p.content, p.like_count
		FROM posts p
		JOIN users u ON u.id = p.user_id
		ORDER BY p.like_count DESC, p.id DESC
		LIMIT $1 OFFSET $2`, pageSize+1, offset)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}
	defer rows.Close()

	items := make([]trendingPost, 0, pageSize)
	for rows.Next() {
		var item trendingPost
		if err := rows.Scan(&item.ID, &item.Username, &item.Content, &item.LikeCount); err != nil {
			writeError(w, http.StatusInternalServerError, "failed to read trending posts")
			return
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to read trending posts")
		return
	}
	hasNext := len(items) > pageSize
	if hasNext {
		items = items[:pageSize]
	}
	writeJSON(w, http.StatusOK, trendingResponse{Items: items, Page: page, PageSize: pageSize, HasNext: hasNext})
}

func parseLikePath(path string) (int64, bool) {
	if !strings.HasPrefix(path, "/posts/") || !strings.HasSuffix(path, "/like") {
		return 0, false
	}
	idPart := strings.TrimSuffix(strings.TrimPrefix(path, "/posts/"), "/like")
	if idPart == "" || strings.Contains(idPart, "/") {
		return 0, false
	}
	id, err := strconv.ParseInt(idPart, 10, 64)
	if err != nil || id <= 0 {
		return 0, false
	}
	return id, true
}

func parsePage(raw string) (int, bool) {
	if raw == "" {
		return 1, true
	}
	page64, err := strconv.ParseInt(raw, 10, 32)
	if err != nil || page64 < 1 {
		return 0, false
	}
	return int(page64), true
}

func requestContext(r *http.Request) (context.Context, context.CancelFunc) {
	return context.WithTimeout(r.Context(), 5*time.Second)
}

func decodeJSON(w http.ResponseWriter, r *http.Request, dst any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
	decoder := json.NewDecoder(r.Body)
	if err := decoder.Decode(dst); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON")
		return false
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		writeError(w, http.StatusBadRequest, "invalid JSON")
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(payload); err != nil {
		log.Printf("failed to encode response: %v", err)
	}
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, errorResponse{Error: message})
}

func methodNotAllowed(w http.ResponseWriter) {
	writeError(w, http.StatusMethodNotAllowed, "method not allowed")
}
