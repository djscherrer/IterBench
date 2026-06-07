package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
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

	"github.com/lib/pq"
)

const (
	pageSize     = 50
	maxBodyBytes = 1 << 20
)

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

type feedItem struct {
	ID        int64     `json:"id"`
	Username  string    `json:"username"`
	Content   string    `json:"content"`
	CreatedAt time.Time `json:"created_at"`
	LikeCount int       `json:"like_count"`
}

type trendingItem struct {
	ID        int64  `json:"id"`
	Username  string `json:"username"`
	Content   string `json:"content"`
	LikeCount int    `json:"like_count"`
}

type feedResponse struct {
	Items    []feedItem `json:"items"`
	Page     int64      `json:"page"`
	PageSize int        `json:"page_size"`
	HasNext  bool       `json:"has_next"`
}

type trendingResponse struct {
	Items    []trendingItem `json:"items"`
	Page     int64          `json:"page"`
	PageSize int            `json:"page_size"`
	HasNext  bool           `json:"has_next"`
}

type errorResponse struct {
	Error string `json:"error"`
}

func main() {
	db, err := openDB()
	if err != nil {
		log.Fatalf("database initialization failed: %v", err)
	}
	defer db.Close()

	a := &app{db: db}
	mux := http.NewServeMux()
	mux.HandleFunc("/users", a.handleUsers)
	mux.HandleFunc("/posts", a.handlePosts)
	mux.HandleFunc("/posts/", a.handlePostActions)
	mux.HandleFunc("/follow", a.handleFollow)
	mux.HandleFunc("/feed", a.handleFeed)
	mux.HandleFunc("/trending", a.handleTrending)

	port := envOr("PORT", "5001")
	server := &http.Server{
		Addr:              "0.0.0.0:" + port,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       120 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}

	log.Printf("listening on %s", server.Addr)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("server failed: %v", err)
	}
}

func openDB() (*sql.DB, error) {
	dsn := databaseURL()
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}

	maxOpen := runtime.GOMAXPROCS(0) * 16
	if maxOpen < 32 {
		maxOpen = 32
	}
	if maxOpen > 128 {
		maxOpen = 128
	}
	db.SetMaxOpenConns(maxOpen)
	db.SetMaxIdleConns(maxOpen)
	db.SetConnMaxLifetime(5 * time.Minute)
	db.SetConnMaxIdleTime(5 * time.Minute)

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}
	if err := initializeSchema(ctx, db); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

func databaseURL() string {
	u := &url.URL{
		Scheme: "postgres",
		User:   url.UserPassword(envOr("DB_USER", "postgres"), os.Getenv("DB_PASSWORD")),
		Host:   net.JoinHostPort(envOr("DB_HOST", "localhost"), envOr("DB_PORT", "5432")),
		Path:   envOr("DB_NAME", "postgres"),
	}
	q := u.Query()
	q.Set("sslmode", "disable")
	u.RawQuery = q.Encode()
	return u.String()
}

func initializeSchema(ctx context.Context, db *sql.DB) error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS users (
			username TEXT PRIMARY KEY,
			full_name TEXT NOT NULL,
			bio TEXT NOT NULL DEFAULT '',
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			CHECK (length(username) > 0),
			CHECK (length(full_name) > 0)
		)`,
		`CREATE TABLE IF NOT EXISTS posts (
			id BIGSERIAL PRIMARY KEY,
			username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
			content TEXT NOT NULL,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			like_count INTEGER NOT NULL DEFAULT 0 CHECK (like_count >= 0),
			CHECK (length(content) > 0)
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
		`CREATE INDEX IF NOT EXISTS idx_posts_username_created_id ON posts (username, created_at DESC, id DESC)`,
		`CREATE INDEX IF NOT EXISTS idx_posts_trending ON posts (like_count DESC, id DESC)`,
		`CREATE INDEX IF NOT EXISTS idx_follows_following ON follows (following_username, follower_username)`,
		`CREATE INDEX IF NOT EXISTS idx_likes_username ON likes (username, post_id)`,
	}
	for _, stmt := range statements {
		if _, err := db.ExecContext(ctx, stmt); err != nil {
			return fmt.Errorf("schema statement failed: %w", err)
		}
	}
	return nil
}

func (a *app) handleUsers(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/users" {
		notFound(w)
		return
	}
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}

	var req createUserRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	req.Username = strings.TrimSpace(req.Username)
	req.FullName = strings.TrimSpace(req.FullName)
	req.Bio = strings.TrimSpace(req.Bio)
	if req.Username == "" || req.FullName == "" {
		badRequest(w, "username and full_name are required")
		return
	}

	_, err := a.db.ExecContext(r.Context(),
		`INSERT INTO users (username, full_name, bio) VALUES ($1, $2, $3)`,
		req.Username, req.FullName, req.Bio,
	)
	if err != nil {
		if isConstraintError(err) {
			badRequest(w, "invalid input or username already exists")
			return
		}
		internalError(w, err)
		return
	}
	w.WriteHeader(http.StatusCreated)
}

func (a *app) handlePosts(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/posts" {
		notFound(w)
		return
	}
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}

	var req createPostRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	req.Username = strings.TrimSpace(req.Username)
	req.Content = strings.TrimSpace(req.Content)
	if req.Username == "" || req.Content == "" {
		badRequest(w, "username and content are required")
		return
	}

	_, err := a.db.ExecContext(r.Context(),
		`INSERT INTO posts (username, content) VALUES ($1, $2)`,
		req.Username, req.Content,
	)
	if err != nil {
		if isConstraintError(err) {
			badRequest(w, "invalid input")
			return
		}
		internalError(w, err)
		return
	}
	w.WriteHeader(http.StatusCreated)
}

func (a *app) handlePostActions(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}

	postID, matched, ok := parseLikePath(r.URL.Path)
	if !matched {
		notFound(w)
		return
	}
	if !ok {
		badRequest(w, "invalid postId")
		return
	}

	var req likeRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	req.Username = strings.TrimSpace(req.Username)
	if req.Username == "" {
		badRequest(w, "username is required")
		return
	}

	var inserted bool
	err := a.db.QueryRowContext(r.Context(), `
		WITH inserted AS (
			INSERT INTO likes (post_id, username)
			VALUES ($1, $2)
			ON CONFLICT DO NOTHING
			RETURNING 1
		), updated AS (
			UPDATE posts
			SET like_count = like_count + 1
			WHERE id = $1 AND EXISTS (SELECT 1 FROM inserted)
			RETURNING 1
		)
		SELECT EXISTS (SELECT 1 FROM inserted)`, postID, req.Username).Scan(&inserted)
	if err != nil {
		if isConstraintError(err) {
			badRequest(w, "invalid input")
			return
		}
		internalError(w, err)
		return
	}
	if inserted {
		w.WriteHeader(http.StatusCreated)
		return
	}
	w.WriteHeader(http.StatusOK)
}

func (a *app) handleFollow(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/follow" {
		notFound(w)
		return
	}
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}

	var req followRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	req.FollowerUsername = strings.TrimSpace(req.FollowerUsername)
	req.FollowingUsername = strings.TrimSpace(req.FollowingUsername)
	if req.FollowerUsername == "" || req.FollowingUsername == "" {
		badRequest(w, "follower_username and following_username are required")
		return
	}

	res, err := a.db.ExecContext(r.Context(),
		`INSERT INTO follows (follower_username, following_username) VALUES ($1, $2) ON CONFLICT DO NOTHING`,
		req.FollowerUsername, req.FollowingUsername,
	)
	if err != nil {
		if isConstraintError(err) {
			badRequest(w, "invalid input")
			return
		}
		internalError(w, err)
		return
	}
	rows, err := res.RowsAffected()
	if err != nil {
		internalError(w, err)
		return
	}
	if rows == 0 {
		w.WriteHeader(http.StatusOK)
		return
	}
	w.WriteHeader(http.StatusCreated)
}

func (a *app) handleFeed(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/feed" {
		notFound(w)
		return
	}
	if r.Method != http.MethodGet {
		methodNotAllowed(w, http.MethodGet)
		return
	}
	username := strings.TrimSpace(r.URL.Query().Get("username"))
	if username == "" {
		badRequest(w, "username is required")
		return
	}
	page, ok := parsePage(w, r)
	if !ok {
		return
	}
	offset := (page - 1) * pageSize

	rows, err := a.db.QueryContext(r.Context(), `
		SELECT p.id, p.username, p.content, p.created_at, p.like_count
		FROM follows f
		JOIN posts p ON p.username = f.following_username
		WHERE f.follower_username = $1
		ORDER BY p.created_at DESC, p.id DESC
		LIMIT $2 OFFSET $3`, username, pageSize+1, offset)
	if err != nil {
		internalError(w, err)
		return
	}
	defer rows.Close()

	items := make([]feedItem, 0, pageSize)
	for rows.Next() {
		var item feedItem
		if err := rows.Scan(&item.ID, &item.Username, &item.Content, &item.CreatedAt, &item.LikeCount); err != nil {
			internalError(w, err)
			return
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		internalError(w, err)
		return
	}

	hasNext := len(items) > pageSize
	if hasNext {
		items = items[:pageSize]
	}
	writeJSON(w, http.StatusOK, feedResponse{Items: items, Page: page, PageSize: pageSize, HasNext: hasNext})
}

func (a *app) handleTrending(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/trending" {
		notFound(w)
		return
	}
	if r.Method != http.MethodGet {
		methodNotAllowed(w, http.MethodGet)
		return
	}
	page, ok := parsePage(w, r)
	if !ok {
		return
	}
	offset := (page - 1) * pageSize

	rows, err := a.db.QueryContext(r.Context(), `
		SELECT id, username, content, like_count
		FROM posts
		ORDER BY like_count DESC, id DESC
		LIMIT $1 OFFSET $2`, pageSize+1, offset)
	if err != nil {
		internalError(w, err)
		return
	}
	defer rows.Close()

	items := make([]trendingItem, 0, pageSize)
	for rows.Next() {
		var item trendingItem
		if err := rows.Scan(&item.ID, &item.Username, &item.Content, &item.LikeCount); err != nil {
			internalError(w, err)
			return
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		internalError(w, err)
		return
	}

	hasNext := len(items) > pageSize
	if hasNext {
		items = items[:pageSize]
	}
	writeJSON(w, http.StatusOK, trendingResponse{Items: items, Page: page, PageSize: pageSize, HasNext: hasNext})
}

func decodeJSON(w http.ResponseWriter, r *http.Request, dst any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)
	defer r.Body.Close()

	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(dst); err != nil {
		badRequest(w, "invalid JSON body")
		return false
	}
	var extra any
	if err := dec.Decode(&extra); err != nil && !errors.Is(err, io.EOF) {
		badRequest(w, "invalid JSON body")
		return false
	} else if err == nil {
		badRequest(w, "invalid JSON body")
		return false
	}
	return true
}

func parseLikePath(path string) (int64, bool, bool) {
	if !strings.HasPrefix(path, "/posts/") || !strings.HasSuffix(path, "/like") {
		return 0, false, false
	}
	idPart := strings.TrimSuffix(strings.TrimPrefix(path, "/posts/"), "/like")
	if idPart == "" || strings.Contains(idPart, "/") {
		return 0, true, false
	}
	id, err := strconv.ParseInt(idPart, 10, 64)
	if err != nil || id <= 0 {
		return 0, true, false
	}
	return id, true, true
}

func parsePage(w http.ResponseWriter, r *http.Request) (int64, bool) {
	value := strings.TrimSpace(r.URL.Query().Get("page"))
	if value == "" {
		return 1, true
	}
	page, err := strconv.ParseInt(value, 10, 64)
	if err != nil || page < 1 {
		badRequest(w, "page must be a positive integer")
		return 0, false
	}
	return page, true
}

func isConstraintError(err error) bool {
	var pqErr *pq.Error
	if !errors.As(err, &pqErr) {
		return false
	}
	switch pqErr.Code {
	case "23502", "23503", "23505", "23514":
		return true
	default:
		return false
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("failed to write response: %v", err)
	}
}

func badRequest(w http.ResponseWriter, message string) {
	writeJSON(w, http.StatusBadRequest, errorResponse{Error: message})
}

func notFound(w http.ResponseWriter) {
	writeJSON(w, http.StatusNotFound, errorResponse{Error: "not found"})
}

func methodNotAllowed(w http.ResponseWriter, allowed string) {
	w.Header().Set("Allow", allowed)
	writeJSON(w, http.StatusMethodNotAllowed, errorResponse{Error: "method not allowed"})
}

func internalError(w http.ResponseWriter, err error) {
	log.Printf("internal error: %v", err)
	writeJSON(w, http.StatusInternalServerError, errorResponse{Error: "internal server error"})
}

func envOr(name, fallback string) string {
	value := os.Getenv(name)
	if value == "" {
		return fallback
	}
	return value
}
