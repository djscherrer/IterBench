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
	"runtime"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/lib/pq"
)

const (
	feedLimit     = 20
	trendingLimit = 10
)

type app struct {
	db             *sql.DB
	createUserStmt *sql.Stmt
	createPostStmt *sql.Stmt
	followStmt     *sql.Stmt
	likeStmt       *sql.Stmt
	feedStmt       *sql.Stmt
	trendingStmt   *sql.Stmt
	trendingCache  atomic.Pointer[cachedTrending]
}

type cachedTrending struct {
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
	LikeCount int       `json:"like_count"`
	CreatedAt time.Time `json:"created_at"`
}

type trendingPost struct {
	ID        int64  `json:"id"`
	Username  string `json:"username"`
	Content   string `json:"content"`
	LikeCount int    `json:"like_count"`
}

type errorResponse struct {
	Error string `json:"error"`
}

func main() {
	logger := log.New(os.Stdout, "", log.LstdFlags|log.LUTC)

	db, err := openDB()
	if err != nil {
		logger.Fatalf("database connection failed: %v", err)
	}
	defer db.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := initSchema(ctx, db); err != nil {
		logger.Fatalf("database initialization failed: %v", err)
	}

	api, err := newApp(db)
	if err != nil {
		logger.Fatalf("application initialization failed: %v", err)
	}
	defer api.close()

	port := envOrDefault("PORT", "5001")
	server := &http.Server{
		Addr:              "0.0.0.0:" + port,
		Handler:           withRecovery(api.routes()),
		ReadHeaderTimeout: 2 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      60 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}

	logger.Printf("listening on 0.0.0.0:%s", port)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		logger.Fatalf("server failed: %v", err)
	}
}

func openDB() (*sql.DB, error) {
	host, err := requiredEnv("DB_HOST")
	if err != nil {
		return nil, err
	}
	port, err := requiredEnv("DB_PORT")
	if err != nil {
		return nil, err
	}
	user, err := requiredEnv("DB_USER")
	if err != nil {
		return nil, err
	}
	password, err := requiredEnv("DB_PASSWORD")
	if err != nil {
		return nil, err
	}
	name, err := requiredEnv("DB_NAME")
	if err != nil {
		return nil, err
	}

	dsn := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable application_name=microblog_api",
		host, port, user, password, name)
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}

	maxOpen := runtime.NumCPU() * 16
	if maxOpen < 32 {
		maxOpen = 32
	}
	if maxOpen > 128 {
		maxOpen = 128
	}
	db.SetMaxOpenConns(maxOpen)
	db.SetMaxIdleConns(maxOpen)
	db.SetConnMaxIdleTime(5 * time.Minute)
	db.SetConnMaxLifetime(30 * time.Minute)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}

	return db, nil
}

func initSchema(ctx context.Context, db *sql.DB) error {
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
	like_count INTEGER NOT NULL DEFAULT 0,
	created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
	ON posts (like_count DESC, created_at DESC, id DESC)
	INCLUDE (username, content);
`
	_, err := db.ExecContext(ctx, schema)
	return err
}

func newApp(db *sql.DB) (*app, error) {
	createUserStmt, err := db.Prepare(`INSERT INTO users (username, full_name, bio) VALUES ($1, $2, $3)`)
	if err != nil {
		return nil, err
	}
	createPostStmt, err := db.Prepare(`INSERT INTO posts (username, content) VALUES ($1, $2)`)
	if err != nil {
		createUserStmt.Close()
		return nil, err
	}
	followStmt, err := db.Prepare(`INSERT INTO follows (follower_username, following_username) VALUES ($1, $2) ON CONFLICT DO NOTHING`)
	if err != nil {
		createUserStmt.Close()
		createPostStmt.Close()
		return nil, err
	}
	likeStmt, err := db.Prepare(`
WITH inserted AS (
	INSERT INTO likes (post_id, username)
	VALUES ($1, $2)
	ON CONFLICT DO NOTHING
	RETURNING 1
)
UPDATE posts
SET like_count = like_count + (SELECT COUNT(*) FROM inserted)
WHERE id = $1
RETURNING id`)
	if err != nil {
		createUserStmt.Close()
		createPostStmt.Close()
		followStmt.Close()
		return nil, err
	}
	feedStmt, err := db.Prepare(`
SELECT p.id, p.username, p.content, p.like_count, p.created_at
FROM posts p
JOIN (
	SELECT following_username AS username
	FROM follows
	WHERE follower_username = $1
	UNION ALL
	SELECT $1::TEXT
) u ON u.username = p.username
ORDER BY p.created_at DESC, p.id DESC
LIMIT 20`)
	if err != nil {
		createUserStmt.Close()
		createPostStmt.Close()
		followStmt.Close()
		likeStmt.Close()
		return nil, err
	}
	trendingStmt, err := db.Prepare(`
SELECT id, username, content, like_count
FROM posts
ORDER BY like_count DESC, created_at DESC, id DESC
LIMIT 10`)
	if err != nil {
		createUserStmt.Close()
		createPostStmt.Close()
		followStmt.Close()
		likeStmt.Close()
		feedStmt.Close()
		return nil, err
	}

	api := &app{
		db:             db,
		createUserStmt: createUserStmt,
		createPostStmt: createPostStmt,
		followStmt:     followStmt,
		likeStmt:       likeStmt,
		feedStmt:       feedStmt,
		trendingStmt:   trendingStmt,
	}
	api.trendingCache.Store((*cachedTrending)(nil))
	return api, nil
}

func (a *app) close() {
	if a.createUserStmt != nil {
		a.createUserStmt.Close()
	}
	if a.createPostStmt != nil {
		a.createPostStmt.Close()
	}
	if a.followStmt != nil {
		a.followStmt.Close()
	}
	if a.likeStmt != nil {
		a.likeStmt.Close()
	}
	if a.feedStmt != nil {
		a.feedStmt.Close()
	}
	if a.trendingStmt != nil {
		a.trendingStmt.Close()
	}
}

func (a *app) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /users", a.handleCreateUser)
	mux.HandleFunc("POST /posts", a.handleCreatePost)
	mux.HandleFunc("POST /follow", a.handleFollow)
	mux.HandleFunc("POST /posts/{postId}/like", a.handleLikePost)
	mux.HandleFunc("GET /feed", a.handleFeed)
	mux.HandleFunc("GET /trending", a.handleTrending)
	return mux
}

func (a *app) handleCreateUser(w http.ResponseWriter, r *http.Request) {
	var req userRequest
	if !decodeJSON(w, r, &req) {
		return
	}

	req.Username = normalize(req.Username)
	req.FullName = strings.TrimSpace(req.FullName)
	req.Bio = strings.TrimSpace(req.Bio)
	if req.Username == "" || req.FullName == "" || len(req.Username) > 64 || len(req.FullName) > 128 || len(req.Bio) > 1024 {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	_, err := a.createUserStmt.ExecContext(r.Context(), req.Username, req.FullName, req.Bio)
	if err != nil {
		if isPGCode(err, "23505") {
			writeError(w, http.StatusBadRequest, "username already exists")
			return
		}
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func (a *app) handleCreatePost(w http.ResponseWriter, r *http.Request) {
	var req postRequest
	if !decodeJSON(w, r, &req) {
		return
	}

	req.Username = normalize(req.Username)
	req.Content = strings.TrimSpace(req.Content)
	if req.Username == "" || req.Content == "" || len(req.Username) > 64 || len(req.Content) > 4096 {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	_, err := a.createPostStmt.ExecContext(r.Context(), req.Username, req.Content)
	if err != nil {
		if isPGCode(err, "23503") {
			writeError(w, http.StatusBadRequest, "invalid input")
			return
		}
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func (a *app) handleFollow(w http.ResponseWriter, r *http.Request) {
	var req followRequest
	if !decodeJSON(w, r, &req) {
		return
	}

	req.FollowerUsername = normalize(req.FollowerUsername)
	req.FollowingUsername = normalize(req.FollowingUsername)
	if req.FollowerUsername == "" || req.FollowingUsername == "" || len(req.FollowerUsername) > 64 || len(req.FollowingUsername) > 64 {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	_, err := a.followStmt.ExecContext(r.Context(), req.FollowerUsername, req.FollowingUsername)
	if err != nil {
		if isPGCode(err, "23503") {
			writeError(w, http.StatusBadRequest, "invalid input")
			return
		}
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func (a *app) handleLikePost(w http.ResponseWriter, r *http.Request) {
	postID, err := strconv.ParseInt(r.PathValue("postId"), 10, 64)
	if err != nil || postID <= 0 {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	var req likeRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	username := normalize(req.Username)
	if username == "" || len(username) > 64 {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	var updatedID int64
	err = a.likeStmt.QueryRowContext(r.Context(), postID, username).Scan(&updatedID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) || isPGCode(err, "23503") {
			writeError(w, http.StatusBadRequest, "invalid input")
			return
		}
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func (a *app) handleFeed(w http.ResponseWriter, r *http.Request) {
	username := normalize(r.URL.Query().Get("username"))
	if username == "" || len(username) > 64 {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}

	rows, err := a.feedStmt.QueryContext(r.Context(), username)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	defer rows.Close()

	posts := make([]feedPost, 0, feedLimit)
	for rows.Next() {
		var post feedPost
		if err := rows.Scan(&post.ID, &post.Username, &post.Content, &post.LikeCount, &post.CreatedAt); err != nil {
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
	now := time.Now().UnixNano()
	if cached := a.trendingCache.Load(); cached != nil && now < cached.expiresAt {
		writeRawJSON(w, http.StatusOK, cached.body)
		return
	}

	rows, err := a.trendingStmt.QueryContext(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	defer rows.Close()

	posts := make([]trendingPost, 0, trendingLimit)
	for rows.Next() {
		var post trendingPost
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

	body, err := json.Marshal(posts)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	a.trendingCache.Store(&cachedTrending{
		expiresAt: time.Now().Add(2 * time.Second).UnixNano(),
		body:      body,
	})
	writeRawJSON(w, http.StatusOK, body)
}

func decodeJSON(w http.ResponseWriter, r *http.Request, dst any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
	defer r.Body.Close()

	if err := json.NewDecoder(r.Body).Decode(dst); err != nil {
		writeError(w, http.StatusBadRequest, "invalid input")
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	body, err := json.Marshal(v)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	writeRawJSON(w, status, body)
}

func writeRawJSON(w http.ResponseWriter, status int, body []byte) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, errorResponse{Error: message})
}

func withRecovery(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if recover() != nil {
				writeError(w, http.StatusInternalServerError, "internal server error")
			}
		}()
		next.ServeHTTP(w, r)
	})
}

func normalize(value string) string {
	return strings.TrimSpace(value)
}

func requiredEnv(key string) (string, error) {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return "", fmt.Errorf("%s is required", key)
	}
	return value, nil
}

func envOrDefault(key, fallback string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	return value
}

func isPGCode(err error, code string) bool {
	var pgErr *pq.Error
	return errors.As(err, &pgErr) && string(pgErr.Code) == code
}
