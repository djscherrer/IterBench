package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"runtime"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/lib/pq"
)

const (
	requestBodyLimit = 1 << 20
	feedLimit        = 50
	trendingLimit    = 20
	trendingCacheTTL = 2 * time.Second
)

type app struct {
	db *sql.DB

	createUserStmt *sql.Stmt
	createPostStmt *sql.Stmt
	followStmt     *sql.Stmt
	likeStmt       *sql.Stmt
	feedStmt       *sql.Stmt
	trendingStmt   *sql.Stmt

	trendingCache atomic.Value
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

type postResponse struct {
	ID        int64     `json:"id"`
	Username  string    `json:"username"`
	Content   string    `json:"content"`
	CreatedAt time.Time `json:"created_at"`
	LikeCount int       `json:"like_count"`
}

type trendingPostResponse struct {
	ID        int64  `json:"id"`
	Username  string `json:"username"`
	Content   string `json:"content"`
	LikeCount int    `json:"like_count"`
}

type errorResponse struct {
	Error string `json:"error"`
}

type trendingSnapshot struct {
	ExpiresAt time.Time
	Posts     []trendingPostResponse
}

func main() {
	startupCtx, startupCancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer startupCancel()

	db, err := openDB(startupCtx)
	if err != nil {
		log.Fatalf("open database: %v", err)
	}
	defer db.Close()

	if err := initSchema(startupCtx, db); err != nil {
		log.Fatalf("initialize schema: %v", err)
	}

	application, err := newApp(startupCtx, db)
	if err != nil {
		log.Fatalf("prepare application: %v", err)
	}
	defer application.close()

	port := envOrDefault("PORT", "5001")
	server := &http.Server{
		Addr:              "0.0.0.0:" + port,
		Handler:           application.routes(),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	log.Printf("listening on %s", server.Addr)
	err = server.ListenAndServe()
	if err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("server error: %v", err)
	}
}

func openDB(ctx context.Context) (*sql.DB, error) {
	host := envOrDefault("DB_HOST", "localhost")
	port := envOrDefault("DB_PORT", "5432")
	user := envOrDefault("DB_USER", "postgres")
	password := os.Getenv("DB_PASSWORD")
	dbName := envOrDefault("DB_NAME", "postgres")
	sslMode := envOrDefault("DB_SSLMODE", "disable")

	dsn := fmt.Sprintf(
		"host=%s port=%s user=%s password=%s dbname=%s sslmode=%s connect_timeout=5 application_name=microblog_api",
		host,
		port,
		user,
		password,
		dbName,
		sslMode,
	)

	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}

	maxOpenConns := runtime.GOMAXPROCS(0) * 4
	if maxOpenConns < 16 {
		maxOpenConns = 16
	}
	if maxOpenConns > 128 {
		maxOpenConns = 128
	}

	db.SetMaxOpenConns(maxOpenConns)
	db.SetMaxIdleConns(maxOpenConns / 2)
	db.SetConnMaxLifetime(30 * time.Minute)
	db.SetConnMaxIdleTime(5 * time.Minute)

	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}

	return db, nil
}

func initSchema(ctx context.Context, db *sql.DB) error {
	schema := `
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    full_name TEXT NOT NULL,
    bio TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS posts (
    id BIGSERIAL PRIMARY KEY,
    username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    content TEXT NOT NULL,
    like_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS follows (
    follower_username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    following_username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (follower_username, following_username),
    CHECK (follower_username <> following_username)
);

CREATE TABLE IF NOT EXISTS post_likes (
    post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (post_id, username)
);

CREATE INDEX IF NOT EXISTS idx_posts_username_created_at
    ON posts (username, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_posts_trending
    ON posts (like_count DESC, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_follows_follower_following
    ON follows (follower_username, following_username);

CREATE INDEX IF NOT EXISTS idx_follows_following_follower
    ON follows (following_username, follower_username);

CREATE INDEX IF NOT EXISTS idx_post_likes_username_post
    ON post_likes (username, post_id);
`

	_, err := db.ExecContext(ctx, schema)
	return err
}

func newApp(ctx context.Context, db *sql.DB) (*app, error) {
	createUserStmt, err := db.PrepareContext(ctx, `
INSERT INTO users (username, full_name, bio)
VALUES ($1, $2, $3)
RETURNING username`)
	if err != nil {
		return nil, err
	}

	createPostStmt, err := db.PrepareContext(ctx, `
INSERT INTO posts (username, content)
SELECT $1, $2
WHERE EXISTS (SELECT 1 FROM users WHERE username = $1)
RETURNING id, created_at`)
	if err != nil {
		createUserStmt.Close()
		return nil, err
	}

	followStmt, err := db.PrepareContext(ctx, `
WITH follower_exists AS (
    SELECT 1 FROM users WHERE username = $1
), following_exists AS (
    SELECT 1 FROM users WHERE username = $2
), inserted AS (
    INSERT INTO follows (follower_username, following_username)
    SELECT $1, $2
    WHERE $1 <> $2
      AND EXISTS (SELECT 1 FROM follower_exists)
      AND EXISTS (SELECT 1 FROM following_exists)
    ON CONFLICT DO NOTHING
    RETURNING 1
)
SELECT
    EXISTS (SELECT 1 FROM follower_exists),
    EXISTS (SELECT 1 FROM following_exists),
    EXISTS (SELECT 1 FROM inserted)`)
	if err != nil {
		createUserStmt.Close()
		createPostStmt.Close()
		return nil, err
	}

	likeStmt, err := db.PrepareContext(ctx, `
WITH post_exists AS (
    SELECT 1 FROM posts WHERE id = $1
), user_exists AS (
    SELECT 1 FROM users WHERE username = $2
), inserted AS (
    INSERT INTO post_likes (post_id, username)
    SELECT $1, $2
    WHERE EXISTS (SELECT 1 FROM post_exists)
      AND EXISTS (SELECT 1 FROM user_exists)
    ON CONFLICT DO NOTHING
    RETURNING 1
), updated AS (
    UPDATE posts
    SET like_count = like_count + (SELECT COUNT(*) FROM inserted)
    WHERE id = $1
    RETURNING id
)
SELECT
    EXISTS (SELECT 1 FROM post_exists),
    EXISTS (SELECT 1 FROM user_exists),
    EXISTS (SELECT 1 FROM inserted)`)
	if err != nil {
		createUserStmt.Close()
		createPostStmt.Close()
		followStmt.Close()
		return nil, err
	}

	feedStmt, err := db.PrepareContext(ctx, `
(
    SELECT p.id, p.username, p.content, p.created_at, p.like_count
    FROM posts p
    WHERE p.username = $1
)
UNION ALL
(
    SELECT p.id, p.username, p.content, p.created_at, p.like_count
    FROM follows f
    JOIN posts p ON p.username = f.following_username
    WHERE f.follower_username = $1
)
ORDER BY created_at DESC, id DESC
LIMIT $2`)
	if err != nil {
		createUserStmt.Close()
		createPostStmt.Close()
		followStmt.Close()
		likeStmt.Close()
		return nil, err
	}

	trendingStmt, err := db.PrepareContext(ctx, `
SELECT id, username, content, like_count
FROM posts
ORDER BY like_count DESC, created_at DESC, id DESC
LIMIT $1`)
	if err != nil {
		createUserStmt.Close()
		createPostStmt.Close()
		followStmt.Close()
		likeStmt.Close()
		feedStmt.Close()
		return nil, err
	}

	application := &app{
		db:             db,
		createUserStmt: createUserStmt,
		createPostStmt: createPostStmt,
		followStmt:     followStmt,
		likeStmt:       likeStmt,
		feedStmt:       feedStmt,
		trendingStmt:   trendingStmt,
	}
	application.trendingCache.Store(trendingSnapshot{})

	return application, nil
}

func (a *app) close() {
	if a.trendingStmt != nil {
		a.trendingStmt.Close()
	}
	if a.feedStmt != nil {
		a.feedStmt.Close()
	}
	if a.likeStmt != nil {
		a.likeStmt.Close()
	}
	if a.followStmt != nil {
		a.followStmt.Close()
	}
	if a.createPostStmt != nil {
		a.createPostStmt.Close()
	}
	if a.createUserStmt != nil {
		a.createUserStmt.Close()
	}
}

func (a *app) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/users", a.handleUsers)
	mux.HandleFunc("/posts", a.handlePosts)
	mux.HandleFunc("/posts/", a.handlePostActions)
	mux.HandleFunc("/follow", a.handleFollow)
	mux.HandleFunc("/feed", a.handleFeed)
	mux.HandleFunc("/trending", a.handleTrending)
	return mux
}

func (a *app) handleUsers(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/users" {
		http.NotFound(w, r)
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
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	var username string
	err := a.createUserStmt.QueryRowContext(r.Context(), req.Username, req.FullName, req.Bio).Scan(&username)
	if err != nil {
		var pqErr *pq.Error
		if errors.As(err, &pqErr) && pqErr.Code == "23505" {
			writeError(w, http.StatusBadRequest, "invalid input or username already exists")
			return
		}
		writeError(w, http.StatusInternalServerError, "internal server error")
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
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	var postID int64
	var createdAt time.Time
	err := a.createPostStmt.QueryRowContext(r.Context(), req.Username, req.Content).Scan(&postID, &createdAt)
	if errors.Is(err, sql.ErrNoRows) {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	a.invalidateTrendingCache()
	w.WriteHeader(http.StatusCreated)
}

func (a *app) handlePostActions(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}

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
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	var postExists bool
	var userExists bool
	var inserted bool
	err := a.likeStmt.QueryRowContext(r.Context(), postID, req.Username).Scan(&postExists, &userExists, &inserted)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	if !postExists || !userExists {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}
	if inserted {
		a.invalidateTrendingCache()
	}

	w.WriteHeader(http.StatusCreated)
}

func (a *app) handleFollow(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/follow" {
		http.NotFound(w, r)
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
	if req.FollowerUsername == "" || req.FollowingUsername == "" || req.FollowerUsername == req.FollowingUsername {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	var followerExists bool
	var followingExists bool
	var inserted bool
	err := a.followStmt.QueryRowContext(r.Context(), req.FollowerUsername, req.FollowingUsername).Scan(&followerExists, &followingExists, &inserted)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	if !followerExists || !followingExists {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func (a *app) handleFeed(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/feed" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodGet {
		methodNotAllowed(w, http.MethodGet)
		return
	}

	username := strings.TrimSpace(r.URL.Query().Get("username"))
	if username == "" {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	rows, err := a.feedStmt.QueryContext(r.Context(), username, feedLimit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	defer rows.Close()

	posts := make([]postResponse, 0, feedLimit)
	for rows.Next() {
		var post postResponse
		if err := rows.Scan(&post.ID, &post.Username, &post.Content, &post.CreatedAt, &post.LikeCount); err != nil {
			writeError(w, http.StatusInternalServerError, "internal server error")
			return
		}
		posts = append(posts, post)
	}
	if err := rows.Err(); err != nil {
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	writeJSON(w, http.StatusOK, posts)
}

func (a *app) handleTrending(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/trending" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodGet {
		methodNotAllowed(w, http.MethodGet)
		return
	}

	now := time.Now()
	cachedValue := a.trendingCache.Load()
	if cachedValue != nil {
		snapshot := cachedValue.(trendingSnapshot)
		if !snapshot.ExpiresAt.IsZero() && now.Before(snapshot.ExpiresAt) {
			writeJSON(w, http.StatusOK, snapshot.Posts)
			return
		}
	}

	rows, err := a.trendingStmt.QueryContext(r.Context(), trendingLimit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	defer rows.Close()

	posts := make([]trendingPostResponse, 0, trendingLimit)
	for rows.Next() {
		var post trendingPostResponse
		if err := rows.Scan(&post.ID, &post.Username, &post.Content, &post.LikeCount); err != nil {
			writeError(w, http.StatusInternalServerError, "internal server error")
			return
		}
		posts = append(posts, post)
	}
	if err := rows.Err(); err != nil {
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	a.trendingCache.Store(trendingSnapshot{
		ExpiresAt: now.Add(trendingCacheTTL),
		Posts:     posts,
	})
	writeJSON(w, http.StatusOK, posts)
}

func (a *app) invalidateTrendingCache() {
	a.trendingCache.Store(trendingSnapshot{})
}

func parseLikePath(path string) (int64, bool) {
	if !strings.HasPrefix(path, "/posts/") || !strings.HasSuffix(path, "/like") {
		return 0, false
	}

	trimmed := strings.TrimPrefix(path, "/posts/")
	parts := strings.Split(trimmed, "/")
	if len(parts) != 2 || parts[0] == "" || parts[1] != "like" {
		return 0, false
	}

	postID, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil || postID <= 0 {
		return 0, false
	}

	return postID, true
}

func decodeJSON(w http.ResponseWriter, r *http.Request, dst any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, requestBodyLimit)
	defer r.Body.Close()

	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()

	if err := decoder.Decode(dst); err != nil {
		writeError(w, http.StatusBadRequest, "invalid input")
		return false
	}

	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		writeError(w, http.StatusBadRequest, "invalid input")
		return false
	}

	return true
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(payload); err != nil {
		log.Printf("encode response: %v", err)
	}
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, errorResponse{Error: message})
}

func methodNotAllowed(w http.ResponseWriter, allowed string) {
	w.Header().Set("Allow", allowed)
	writeError(w, http.StatusMethodNotAllowed, "method not allowed")
}

func envOrDefault(key, fallback string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	return value
}
