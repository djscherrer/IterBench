package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/lib/pq"
)

const (
	pageSize = 50
	cacheTTL = 5 * time.Second
)

type application struct {
	db *sql.DB

	createUser        *sql.Stmt
	createPostKnown   *sql.Stmt
	createPostChecked *sql.Stmt
	insertFollow      *sql.Stmt
	insertLike        *sql.Stmt
	feed              *sql.Stmt
	trending          *sql.Stmt
	userExists        *sql.Stmt
	postExists        *sql.Stmt

	users     sync.Map
	posts     sync.Map
	follows   sync.Map
	followers sync.Map
	likes     sync.Map

	feedCache     sync.Map
	trendingCache sync.Map
}

type cacheEntry struct {
	expiresAt int64
	body      []byte
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

type followKey struct {
	follower  string
	following string
}

type likeKey struct {
	postID   int64
	username string
}

func main() {
	app, err := openApplication()
	if err != nil {
		log.Fatalf("database initialization failed: %v", err)
	}
	defer app.db.Close()

	mux := http.NewServeMux()
	mux.HandleFunc("/users", app.handleUsers)
	mux.HandleFunc("/posts", app.handlePosts)
	mux.HandleFunc("/posts/", app.handlePostActions)
	mux.HandleFunc("/follow", app.handleFollow)
	mux.HandleFunc("/feed", app.handleFeed)
	mux.HandleFunc("/trending", app.handleTrending)

	port := getenv("PORT", "5001")
	server := &http.Server{
		Addr:              "0.0.0.0:" + port,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	log.Printf("listening on 0.0.0.0:%s", port)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("server failed: %v", err)
	}
}

func openApplication() (*application, error) {
	dsn := fmt.Sprintf(
		"host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		getenv("DB_HOST", "localhost"),
		getenv("DB_PORT", "5432"),
		getenv("DB_USER", "postgres"),
		getenv("DB_PASSWORD", ""),
		getenv("DB_NAME", "postgres"),
	)

	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}

	maxOpen := intEnv("DB_MAX_OPEN_CONNS", 96)
	if maxOpen < 1 {
		maxOpen = 96
	}
	db.SetMaxOpenConns(maxOpen)
	db.SetMaxIdleConns(maxOpen)
	db.SetConnMaxLifetime(10 * time.Minute)
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

	app := &application{db: db}
	if err := app.prepare(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return app, nil
}

func initializeSchema(ctx context.Context, db *sql.DB) error {
	const schema = `
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
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    like_count INTEGER NOT NULL DEFAULT 0 CHECK (like_count >= 0)
);

CREATE TABLE IF NOT EXISTS follows (
    follower_username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    following_username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (follower_username, following_username)
);

CREATE TABLE IF NOT EXISTS likes (
    post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (post_id, username)
);

CREATE INDEX IF NOT EXISTS idx_posts_feed_covering
    ON posts (username, created_at DESC, id DESC)
    INCLUDE (content, like_count);
CREATE INDEX IF NOT EXISTS idx_posts_trending_covering
    ON posts (like_count DESC, id DESC)
    INCLUDE (username, content);
CREATE INDEX IF NOT EXISTS idx_follows_follower_following ON follows (follower_username, following_username);
CREATE INDEX IF NOT EXISTS idx_likes_username ON likes (username);
`
	_, err := db.ExecContext(ctx, schema)
	return err
}

func (app *application) prepare(ctx context.Context) error {
	stmts := []struct {
		dst **sql.Stmt
		sql string
	}{
		{&app.createUser, `INSERT INTO users (username, full_name, bio) VALUES ($1, $2, $3)`},
		{&app.createPostKnown, `INSERT INTO posts (username, content) VALUES ($1, $2) RETURNING id, created_at, like_count`},
		{&app.createPostChecked, `
INSERT INTO posts (username, content)
SELECT $1, $2
WHERE EXISTS (SELECT 1 FROM users WHERE username = $1)
RETURNING id, created_at, like_count`},
		{&app.insertFollow, `INSERT INTO follows (follower_username, following_username) VALUES ($1, $2) ON CONFLICT DO NOTHING`},
		{&app.insertLike, `
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
SELECT EXISTS (SELECT 1 FROM inserted)`},
		{&app.feed, `
SELECT p.id, p.username, p.content, p.created_at, p.like_count
FROM follows f
JOIN posts p ON p.username = f.following_username
WHERE f.follower_username = $1
ORDER BY p.created_at DESC, p.id DESC
LIMIT $2 OFFSET $3`},
		{&app.trending, `
SELECT id, username, content, like_count
FROM posts
ORDER BY like_count DESC, id DESC
LIMIT $1 OFFSET $2`},
		{&app.userExists, `SELECT EXISTS (SELECT 1 FROM users WHERE username = $1)`},
		{&app.postExists, `SELECT EXISTS (SELECT 1 FROM posts WHERE id = $1)`},
	}
	for _, item := range stmts {
		stmt, err := app.db.PrepareContext(ctx, item.sql)
		if err != nil {
			return err
		}
		*item.dst = stmt
	}
	return nil
}

func (app *application) handleUsers(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/users" {
		notFound(w)
		return
	}
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}

	var req userRequest
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

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()
	_, err := app.createUser.ExecContext(ctx, req.Username, req.FullName, req.Bio)
	if err != nil {
		if isConstraintError(err) {
			badRequest(w, "invalid input or username already exists")
			return
		}
		serverError(w, err)
		return
	}
	app.users.Store(req.Username, struct{}{})
	writeJSON(w, http.StatusCreated, req)
}

func (app *application) handlePosts(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/posts" {
		notFound(w)
		return
	}
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}

	var req postRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	req.Username = strings.TrimSpace(req.Username)
	req.Content = strings.TrimSpace(req.Content)
	if req.Username == "" || req.Content == "" {
		badRequest(w, "username and content are required")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()
	var created feedPost
	created.Username = req.Username
	created.Content = req.Content
	var err error
	if app.userCached(req.Username) {
		err = app.createPostKnown.QueryRowContext(ctx, req.Username, req.Content).Scan(&created.ID, &created.CreatedAt, &created.LikeCount)
	} else {
		err = app.createPostChecked.QueryRowContext(ctx, req.Username, req.Content).Scan(&created.ID, &created.CreatedAt, &created.LikeCount)
	}
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) || isConstraintError(err) {
			badRequest(w, "invalid input")
			return
		}
		serverError(w, err)
		return
	}

	app.users.Store(req.Username, struct{}{})
	app.posts.Store(created.ID, struct{}{})
	writeJSON(w, http.StatusCreated, created)
}

func (app *application) handleFollow(w http.ResponseWriter, r *http.Request) {
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

	key := followKey{follower: req.FollowerUsername, following: req.FollowingUsername}
	if _, ok := app.follows.Load(key); ok {
		w.WriteHeader(http.StatusOK)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()
	if !app.userCached(req.FollowerUsername) && !app.userExistsInDB(ctx, req.FollowerUsername) {
		badRequest(w, "invalid input")
		return
	}
	if !app.userCached(req.FollowingUsername) && !app.userExistsInDB(ctx, req.FollowingUsername) {
		badRequest(w, "invalid input")
		return
	}

	res, err := app.insertFollow.ExecContext(ctx, req.FollowerUsername, req.FollowingUsername)
	if err != nil {
		if isConstraintError(err) {
			badRequest(w, "invalid input")
			return
		}
		serverError(w, err)
		return
	}
	rows, _ := res.RowsAffected()
	app.follows.Store(key, struct{}{})
	app.followers.Store(req.FollowerUsername, struct{}{})
	if rows == 0 {
		w.WriteHeader(http.StatusOK)
		return
	}
	w.WriteHeader(http.StatusCreated)
}

func (app *application) handlePostActions(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}
	if !strings.HasPrefix(r.URL.Path, "/posts/") || !strings.HasSuffix(r.URL.Path, "/like") {
		notFound(w)
		return
	}
	postIDText := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/posts/"), "/like")
	if postIDText == "" || strings.Contains(postIDText, "/") {
		notFound(w)
		return
	}
	postID, err := strconv.ParseInt(postIDText, 10, 64)
	if err != nil || postID < 1 {
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

	key := likeKey{postID: postID, username: req.Username}
	if _, ok := app.likes.Load(key); ok {
		w.WriteHeader(http.StatusOK)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()
	if !app.userCached(req.Username) && !app.userExistsInDB(ctx, req.Username) {
		badRequest(w, "invalid input")
		return
	}
	if !app.postCached(postID) && !app.postExistsInDB(ctx, postID) {
		badRequest(w, "invalid input")
		return
	}

	var inserted bool
	err = app.insertLike.QueryRowContext(ctx, postID, req.Username).Scan(&inserted)
	if err != nil {
		if isConstraintError(err) {
			badRequest(w, "invalid input")
			return
		}
		serverError(w, err)
		return
	}
	app.likes.Store(key, struct{}{})
	if inserted {
		w.WriteHeader(http.StatusCreated)
		return
	}
	w.WriteHeader(http.StatusOK)
}

func (app *application) handleFeed(w http.ResponseWriter, r *http.Request) {
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
	page, ok := parsePage(r)
	if !ok {
		badRequest(w, "page must be an integer greater than or equal to 1")
		return
	}
	if _, ok := app.followers.Load(username); !ok {
		body := []byte(fmt.Sprintf(`{"items":[],"page":%d,"page_size":%d,"has_next":false}`, page, pageSize))
		writeRawJSON(w, http.StatusOK, body)
		return
	}
	cacheKey := username + "\x00" + strconv.Itoa(page)
	if body, ok := getCached(&app.feedCache, cacheKey); ok {
		writeRawJSON(w, http.StatusOK, body)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	offset := int64(page-1) * pageSize
	rows, err := app.feed.QueryContext(ctx, username, pageSize+1, offset)
	if err != nil {
		serverError(w, err)
		return
	}
	defer rows.Close()

	items := make([]feedPost, 0, pageSize+1)
	for rows.Next() {
		var item feedPost
		if err := rows.Scan(&item.ID, &item.Username, &item.Content, &item.CreatedAt, &item.LikeCount); err != nil {
			serverError(w, err)
			return
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		serverError(w, err)
		return
	}
	hasNext := len(items) > pageSize
	if hasNext {
		items = items[:pageSize]
	}
	body, err := marshalAndCache(&app.feedCache, cacheKey, feedResponse{Items: items, Page: page, PageSize: pageSize, HasNext: hasNext})
	if err != nil {
		serverError(w, err)
		return
	}
	writeRawJSON(w, http.StatusOK, body)
}

func (app *application) handleTrending(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/trending" {
		notFound(w)
		return
	}
	if r.Method != http.MethodGet {
		methodNotAllowed(w, http.MethodGet)
		return
	}
	page, ok := parsePage(r)
	if !ok {
		badRequest(w, "page must be an integer greater than or equal to 1")
		return
	}
	cacheKey := strconv.Itoa(page)
	if body, ok := getCached(&app.trendingCache, cacheKey); ok {
		writeRawJSON(w, http.StatusOK, body)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	offset := int64(page-1) * pageSize
	rows, err := app.trending.QueryContext(ctx, pageSize+1, offset)
	if err != nil {
		serverError(w, err)
		return
	}
	defer rows.Close()

	items := make([]trendingPost, 0, pageSize+1)
	for rows.Next() {
		var item trendingPost
		if err := rows.Scan(&item.ID, &item.Username, &item.Content, &item.LikeCount); err != nil {
			serverError(w, err)
			return
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		serverError(w, err)
		return
	}
	hasNext := len(items) > pageSize
	if hasNext {
		items = items[:pageSize]
	}
	body, err := marshalAndCache(&app.trendingCache, cacheKey, trendingResponse{Items: items, Page: page, PageSize: pageSize, HasNext: hasNext})
	if err != nil {
		serverError(w, err)
		return
	}
	writeRawJSON(w, http.StatusOK, body)
}

func getCached(cache *sync.Map, key string) ([]byte, bool) {
	value, ok := cache.Load(key)
	if !ok {
		return nil, false
	}
	entry, ok := value.(cacheEntry)
	if !ok || time.Now().UnixNano() >= entry.expiresAt {
		cache.Delete(key)
		return nil, false
	}
	return entry.body, true
}

func marshalAndCache(cache *sync.Map, key string, value any) ([]byte, error) {
	body, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	cache.Store(key, cacheEntry{
		expiresAt: time.Now().Add(cacheTTL).UnixNano(),
		body:      body,
	})
	return body, nil
}

func (app *application) userCached(username string) bool {
	_, ok := app.users.Load(username)
	return ok
}

func (app *application) postCached(postID int64) bool {
	_, ok := app.posts.Load(postID)
	return ok
}

func (app *application) userExistsInDB(ctx context.Context, username string) bool {
	var exists bool
	if err := app.userExists.QueryRowContext(ctx, username).Scan(&exists); err != nil || !exists {
		return false
	}
	app.users.Store(username, struct{}{})
	return true
}

func (app *application) postExistsInDB(ctx context.Context, postID int64) bool {
	var exists bool
	if err := app.postExists.QueryRowContext(ctx, postID).Scan(&exists); err != nil || !exists {
		return false
	}
	app.posts.Store(postID, struct{}{})
	return true
}

func decodeJSON(w http.ResponseWriter, r *http.Request, dst any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
	defer r.Body.Close()
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(dst); err != nil {
		badRequest(w, "invalid JSON body")
		return false
	}
	return true
}

func parsePage(r *http.Request) (int, bool) {
	value := strings.TrimSpace(r.URL.Query().Get("page"))
	if value == "" {
		return 1, true
	}
	page, err := strconv.Atoi(value)
	if err != nil || page < 1 {
		return 0, false
	}
	return page, true
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if value != nil {
		_ = json.NewEncoder(w).Encode(value)
	}
}

func writeRawJSON(w http.ResponseWriter, status int, body []byte) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

func badRequest(w http.ResponseWriter, message string) {
	writeJSON(w, http.StatusBadRequest, map[string]string{"error": message})
}

func notFound(w http.ResponseWriter) {
	writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
}

func methodNotAllowed(w http.ResponseWriter, allowed string) {
	w.Header().Set("Allow", allowed)
	writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
}

func serverError(w http.ResponseWriter, err error) {
	log.Printf("request failed: %v", err)
	writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal server error"})
}

func isConstraintError(err error) bool {
	var pqErr *pq.Error
	if !errors.As(err, &pqErr) {
		return false
	}
	switch string(pqErr.Code) {
	case "23502", "23503", "23505", "23514":
		return true
	default:
		return false
	}
}

func getenv(key, fallback string) string {
	value := os.Getenv(key)
	if value == "" {
		return fallback
	}
	return value
}

func intEnv(key string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return fallback
	}
	return parsed
}
