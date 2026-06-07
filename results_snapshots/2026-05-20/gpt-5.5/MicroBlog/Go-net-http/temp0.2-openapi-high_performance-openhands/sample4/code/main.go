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
	"time"

	"github.com/lib/pq"
)

const pageSize = 50

type application struct {
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
	LikeCount int64     `json:"like_count"`
}

type trendingPost struct {
	ID        int64  `json:"id"`
	Username  string `json:"username"`
	Content   string `json:"content"`
	LikeCount int64  `json:"like_count"`
}

type pageResponse[T any] struct {
	Items    []T  `json:"items"`
	Page     int  `json:"page"`
	PageSize int  `json:"page_size"`
	HasNext  bool `json:"has_next"`
}

type errorResponse struct {
	Error string `json:"error"`
}

func main() {
	db, err := openDatabase()
	if err != nil {
		log.Fatalf("database connection failed: %v", err)
	}
	defer db.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := initSchema(ctx, db); err != nil {
		log.Fatalf("database initialization failed: %v", err)
	}

	app := &application{db: db}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /users", app.createUser)
	mux.HandleFunc("POST /posts", app.createPost)
	mux.HandleFunc("POST /follow", app.followUser)
	mux.HandleFunc("POST /posts/{postId}/like", app.likePost)
	mux.HandleFunc("GET /feed", app.getFeed)
	mux.HandleFunc("GET /trending", app.getTrending)

	port := strings.TrimSpace(os.Getenv("PORT"))
	if port == "" {
		port = "5001"
	}

	server := &http.Server{
		Addr:              "0.0.0.0:" + port,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	log.Printf("listening on %s", server.Addr)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("server failed: %v", err)
	}
}

func openDatabase() (*sql.DB, error) {
	host := strings.TrimSpace(os.Getenv("DB_HOST"))
	port := strings.TrimSpace(os.Getenv("DB_PORT"))
	user := os.Getenv("DB_USER")
	password := os.Getenv("DB_PASSWORD")
	name := strings.TrimSpace(os.Getenv("DB_NAME"))
	if host == "" || port == "" || user == "" || name == "" {
		return nil, errors.New("DB_HOST, DB_PORT, DB_USER, and DB_NAME must be set")
	}

	dsn := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable", host, port, user, password, name)
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}

	maxOpen := getEnvInt("DB_MAX_OPEN_CONNS", runtime.GOMAXPROCS(0)*8)
	if maxOpen < 16 {
		maxOpen = 16
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

func getEnvInt(key string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed <= 0 {
		return fallback
	}
	return parsed
}

func initSchema(ctx context.Context, db *sql.DB) error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS users (
			username TEXT PRIMARY KEY,
			full_name TEXT NOT NULL,
			bio TEXT NOT NULL DEFAULT '',
			created_at TIMESTAMPTZ NOT NULL DEFAULT now()
		)`,
		`CREATE TABLE IF NOT EXISTS posts (
			id BIGSERIAL PRIMARY KEY,
			username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
			content TEXT NOT NULL,
			created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
			like_count BIGINT NOT NULL DEFAULT 0
		)`,
		`CREATE TABLE IF NOT EXISTS follows (
			follower_username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
			following_username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
			created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
			PRIMARY KEY (follower_username, following_username)
		)`,
		`CREATE TABLE IF NOT EXISTS likes (
			post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
			username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
			created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
			PRIMARY KEY (post_id, username)
		)`,
		`CREATE INDEX IF NOT EXISTS idx_posts_username_created_id ON posts (username, created_at DESC, id DESC)`,
		`CREATE INDEX IF NOT EXISTS idx_posts_trending ON posts (like_count DESC, id DESC)`,
		`CREATE INDEX IF NOT EXISTS idx_likes_username ON likes (username)`,
	}
	for _, statement := range statements {
		if _, err := db.ExecContext(ctx, statement); err != nil {
			return err
		}
	}
	return nil
}

func (app *application) createUser(w http.ResponseWriter, r *http.Request) {
	var req userRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	req.Username = strings.TrimSpace(req.Username)
	req.FullName = strings.TrimSpace(req.FullName)
	if req.Username == "" || req.FullName == "" {
		writeError(w, http.StatusBadRequest, "username and full_name are required")
		return
	}

	_, err := app.db.ExecContext(r.Context(), `INSERT INTO users (username, full_name, bio) VALUES ($1, $2, $3)`, req.Username, req.FullName, req.Bio)
	if err != nil {
		if isConstraintViolation(err) {
			writeError(w, http.StatusBadRequest, "invalid input or username already exists")
			return
		}
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	w.WriteHeader(http.StatusCreated)
}

func (app *application) createPost(w http.ResponseWriter, r *http.Request) {
	var req postRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	req.Username = strings.TrimSpace(req.Username)
	if req.Username == "" || strings.TrimSpace(req.Content) == "" {
		writeError(w, http.StatusBadRequest, "username and content are required")
		return
	}

	_, err := app.db.ExecContext(r.Context(), `INSERT INTO posts (username, content) VALUES ($1, $2)`, req.Username, req.Content)
	if err != nil {
		if isConstraintViolation(err) {
			writeError(w, http.StatusBadRequest, "invalid input")
			return
		}
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	w.WriteHeader(http.StatusCreated)
}

func (app *application) followUser(w http.ResponseWriter, r *http.Request) {
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

	var inserted int
	err := app.db.QueryRowContext(r.Context(), `
		INSERT INTO follows (follower_username, following_username)
		VALUES ($1, $2)
		ON CONFLICT DO NOTHING
		RETURNING 1`, req.FollowerUsername, req.FollowingUsername).Scan(&inserted)
	if errors.Is(err, sql.ErrNoRows) {
		w.WriteHeader(http.StatusOK)
		return
	}
	if err != nil {
		if isConstraintViolation(err) {
			writeError(w, http.StatusBadRequest, "invalid input")
			return
		}
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	w.WriteHeader(http.StatusCreated)
}

func (app *application) likePost(w http.ResponseWriter, r *http.Request) {
	postID, err := strconv.ParseInt(r.PathValue("postId"), 10, 64)
	if err != nil || postID <= 0 {
		writeError(w, http.StatusBadRequest, "invalid postId")
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

	var liked bool
	err = app.db.QueryRowContext(r.Context(), `
		WITH inserted AS (
			INSERT INTO likes (post_id, username)
			VALUES ($1, $2)
			ON CONFLICT DO NOTHING
			RETURNING post_id
		), updated AS (
			UPDATE posts p
			SET like_count = p.like_count + 1
			FROM inserted i
			WHERE p.id = i.post_id
			RETURNING 1
		)
		SELECT EXISTS (SELECT 1 FROM inserted)`, postID, req.Username).Scan(&liked)
	if err != nil {
		if isConstraintViolation(err) {
			writeError(w, http.StatusBadRequest, "invalid input")
			return
		}
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	if liked {
		w.WriteHeader(http.StatusCreated)
		return
	}
	w.WriteHeader(http.StatusOK)
}

func (app *application) getFeed(w http.ResponseWriter, r *http.Request) {
	username := strings.TrimSpace(r.URL.Query().Get("username"))
	if username == "" {
		writeError(w, http.StatusBadRequest, "username is required")
		return
	}
	page, ok := parsePage(w, r)
	if !ok {
		return
	}

	limit := pageSize + 1
	offset := (page - 1) * pageSize
	rows, err := app.db.QueryContext(r.Context(), `
		SELECT p.id, p.username, p.content, p.created_at, p.like_count
		FROM posts p
		JOIN follows f ON f.following_username = p.username
		WHERE f.follower_username = $1
		ORDER BY p.created_at DESC, p.id DESC
		LIMIT $2 OFFSET $3`, username, limit, offset)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	defer rows.Close()

	items := make([]feedPost, 0, pageSize)
	for rows.Next() {
		var item feedPost
		if err := rows.Scan(&item.ID, &item.Username, &item.Content, &item.CreatedAt, &item.LikeCount); err != nil {
			writeError(w, http.StatusInternalServerError, "internal server error")
			return
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	hasNext := len(items) > pageSize
	if hasNext {
		items = items[:pageSize]
	}
	writeJSON(w, http.StatusOK, pageResponse[feedPost]{Items: items, Page: page, PageSize: pageSize, HasNext: hasNext})
}

func (app *application) getTrending(w http.ResponseWriter, r *http.Request) {
	page, ok := parsePage(w, r)
	if !ok {
		return
	}

	limit := pageSize + 1
	offset := (page - 1) * pageSize
	rows, err := app.db.QueryContext(r.Context(), `
		SELECT id, username, content, like_count
		FROM posts
		ORDER BY like_count DESC, id DESC
		LIMIT $1 OFFSET $2`, limit, offset)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}
	defer rows.Close()

	items := make([]trendingPost, 0, pageSize)
	for rows.Next() {
		var item trendingPost
		if err := rows.Scan(&item.ID, &item.Username, &item.Content, &item.LikeCount); err != nil {
			writeError(w, http.StatusInternalServerError, "internal server error")
			return
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		writeError(w, http.StatusInternalServerError, "internal server error")
		return
	}

	hasNext := len(items) > pageSize
	if hasNext {
		items = items[:pageSize]
	}
	writeJSON(w, http.StatusOK, pageResponse[trendingPost]{Items: items, Page: page, PageSize: pageSize, HasNext: hasNext})
}

func parsePage(w http.ResponseWriter, r *http.Request) (int, bool) {
	value := strings.TrimSpace(r.URL.Query().Get("page"))
	if value == "" {
		return 1, true
	}
	page, err := strconv.Atoi(value)
	if err != nil || page < 1 {
		writeError(w, http.StatusBadRequest, "page must be a positive integer")
		return 0, false
	}
	return page, true
}

func decodeJSON(w http.ResponseWriter, r *http.Request, dst any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
	decoder := json.NewDecoder(r.Body)
	if err := decoder.Decode(dst); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return false
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		writeError(w, http.StatusBadRequest, "request body must contain a single JSON object")
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(value); err != nil {
		log.Printf("failed to write response: %v", err)
	}
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, errorResponse{Error: message})
}

func isConstraintViolation(err error) bool {
	var pqErr *pq.Error
	if !errors.As(err, &pqErr) {
		return false
	}
	switch pqErr.Code.Name() {
	case "unique_violation", "foreign_key_violation", "check_violation", "not_null_violation":
		return true
	default:
		return false
	}
}
