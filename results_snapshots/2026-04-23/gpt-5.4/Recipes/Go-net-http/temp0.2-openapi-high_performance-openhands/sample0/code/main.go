package main

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"html/template"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/lib/pq"
)

const (
	defaultPort      = "5001"
	overviewLimit    = 20
	overviewCacheTTL = 5 * time.Second
	detailCacheTTL   = 5 * time.Second

	recipeBodyLimit  int64 = 1 << 20
	commentBodyLimit int64 = 1 << 16
	ratingBodyLimit  int64 = 1 << 12
)

var (
	overviewTemplate = template.Must(template.New("overview").Parse(`<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Recipe Overview</title>
</head>
<body>
  <h1>Recipe Overview</h1>
  <section>
    <h2>Recent Recipes</h2>
    <ul>
      {{range .Recent}}
      <li><a href="/recipes/{{.ID}}">{{.Title}}</a></li>
      {{else}}
      <li>No recipes available.</li>
      {{end}}
    </ul>
  </section>
  <section>
    <h2>Top Rated Recipes</h2>
    <ul>
      {{range .TopRated}}
      <li><a href="/recipes/{{.ID}}">{{.Title}}</a></li>
      {{else}}
      <li>No rated recipes available.</li>
      {{end}}
    </ul>
  </section>
</body>
</html>`))
	detailTemplate = template.Must(template.New("detail").Parse(`<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{.Title}}</title>
</head>
<body>
  <nav><a href="/recipes">Back to recipes</a></nav>
  <h1>{{.Title}}</h1>
  {{if .HasRating}}
  <p>Average rating: {{.AvgRating}}</p>
  {{else}}
  <p>No ratings yet.</p>
  {{end}}
  <section>
    <h2>Ingredients</h2>
    <ul>
      {{range .Ingredients}}
      <li>{{.}}</li>
      {{else}}
      <li>No ingredients listed.</li>
      {{end}}
    </ul>
  </section>
  <section>
    <h2>Instructions</h2>
    <p>{{.Instructions}}</p>
  </section>
  <section>
    <h2>Comments</h2>
    <ul>
      {{range .Comments}}
      <li>{{.Comment}}</li>
      {{else}}
      <li>No comments yet.</li>
      {{end}}
    </ul>
  </section>
</body>
</html>`))
)

type app struct {
	db            *sql.DB
	overviewCache htmlCache
	recipeCache   sync.Map
}

type htmlCache struct {
	mu      sync.RWMutex
	body    []byte
	expires time.Time
}

type cachedPage struct {
	body    []byte
	expires int64
}

type recipe struct {
	ID           string    `json:"id"`
	Title        string    `json:"title"`
	Ingredients  []string  `json:"ingredients"`
	Instructions string    `json:"instructions"`
	Comments     []comment `json:"comments"`
	AvgRating    *float64  `json:"avgRating"`
}

type comment struct {
	Comment string `json:"comment"`
}

type uploadRecipeRequest struct {
	Title        string   `json:"title"`
	Ingredients  []string `json:"ingredients"`
	Instructions string   `json:"instructions"`
}

type commentRequest struct {
	Comment string `json:"comment"`
}

type ratingRequest struct {
	Rating int `json:"rating"`
}

type recipeLink struct {
	ID    string
	Title string
}

type overviewPageData struct {
	Recent   []recipeLink
	TopRated []recipeLink
}

type detailPageData struct {
	ID           string
	Title        string
	Ingredients  []string
	Instructions string
	Comments     []comment
	AvgRating    string
	HasRating    bool
}

func main() {
	db, err := openDatabaseFromEnv()
	if err != nil {
		log.Fatalf("open database: %v", err)
	}
	defer db.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	if err := db.PingContext(ctx); err != nil {
		log.Fatalf("ping database: %v", err)
	}
	if err := initSchema(ctx, db); err != nil {
		log.Fatalf("init schema: %v", err)
	}

	application := &app{db: db}
	mux := http.NewServeMux()
	mux.HandleFunc("/recipes", application.handleRecipesOverview)
	mux.HandleFunc("/recipes/upload", application.handleRecipeUpload)
	mux.HandleFunc("/recipes/", application.handleRecipeRoutes)

	port := os.Getenv("PORT")
	if port == "" {
		port = defaultPort
	}

	server := &http.Server{
		Addr:              "0.0.0.0:" + port,
		Handler:           mux,
		ReadHeaderTimeout: 2 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}

	log.Printf("listening on 0.0.0.0:%s", port)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}

func openDatabaseFromEnv() (*sql.DB, error) {
	host := firstNonEmpty(os.Getenv("DB_HOST"), "localhost")
	port := firstNonEmpty(os.Getenv("DB_PORT"), "5432")
	user := firstNonEmpty(os.Getenv("DB_USER"), "postgres")
	password := os.Getenv("DB_PASSWORD")
	databaseName := firstNonEmpty(os.Getenv("DB_NAME"), "postgres")

	dsn := (&url.URL{
		Scheme:   "postgres",
		User:     url.UserPassword(user, password),
		Host:     net.JoinHostPort(host, port),
		Path:     databaseName,
		RawQuery: "sslmode=disable&connect_timeout=5",
	}).String()

	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}

	maxOpenConns := runtime.GOMAXPROCS(0) * 8
	if maxOpenConns < 32 {
		maxOpenConns = 32
	}
	if maxOpenConns > 128 {
		maxOpenConns = 128
	}

	db.SetMaxOpenConns(maxOpenConns)
	db.SetMaxIdleConns(maxOpenConns)
	db.SetConnMaxIdleTime(5 * time.Minute)
	db.SetConnMaxLifetime(30 * time.Minute)

	return db, nil
}

func initSchema(ctx context.Context, db *sql.DB) error {
	const schema = `
CREATE TABLE IF NOT EXISTS recipes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    ingredients TEXT[] NOT NULL,
    instructions TEXT NOT NULL,
    rating_sum BIGINT NOT NULL DEFAULT 0,
    rating_count BIGINT NOT NULL DEFAULT 0,
    avg_rating DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS comments (
    id BIGSERIAL PRIMARY KEY,
    recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    comment TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS recipes_created_at_idx ON recipes (created_at DESC);
CREATE INDEX IF NOT EXISTS recipes_avg_rating_idx ON recipes (avg_rating DESC NULLS LAST, rating_count DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS comments_recipe_id_created_at_idx ON comments (recipe_id, created_at DESC);
`

	_, err := db.ExecContext(ctx, schema)
	return err
}

func (a *app) handleRecipesOverview(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/recipes" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodGet {
		methodNotAllowed(w, http.MethodGet)
		return
	}

	now := time.Now()
	if body, ok := a.overviewCache.get(now); ok {
		writeHTML(w, http.StatusOK, body)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	recent, err := fetchRecipeLinks(ctx, a.db, `
SELECT id, title
FROM recipes
ORDER BY created_at DESC
LIMIT $1`, overviewLimit)
	if err != nil {
		serverError(w)
		return
	}

	topRated, err := fetchRecipeLinks(ctx, a.db, `
SELECT id, title
FROM recipes
WHERE avg_rating IS NOT NULL
ORDER BY avg_rating DESC, rating_count DESC, created_at DESC
LIMIT $1`, overviewLimit)
	if err != nil {
		serverError(w)
		return
	}

	body, err := renderHTML(overviewTemplate, overviewPageData{Recent: recent, TopRated: topRated})
	if err != nil {
		serverError(w)
		return
	}

	a.overviewCache.set(body, overviewCacheTTL)
	writeHTML(w, http.StatusOK, body)
}

func (a *app) handleRecipeUpload(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/recipes/upload" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}

	var req uploadRecipeRequest
	if err := decodeJSON(w, r, &req, recipeBodyLimit); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	title := strings.TrimSpace(req.Title)
	instructions := strings.TrimSpace(req.Instructions)
	ingredients, ok := sanitizeStrings(req.Ingredients)
	if !ok || title == "" || instructions == "" {
		http.Error(w, "title, ingredients, and instructions are required", http.StatusBadRequest)
		return
	}

	recipeID := uuid.NewString()
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	_, err := a.db.ExecContext(ctx, `
INSERT INTO recipes (id, title, ingredients, instructions)
VALUES ($1, $2, $3, $4)`, recipeID, title, pq.Array(ingredients), instructions)
	if err != nil {
		serverError(w)
		return
	}

	a.overviewCache.invalidate()

	response := recipe{
		ID:           recipeID,
		Title:        title,
		Ingredients:  ingredients,
		Instructions: instructions,
		Comments:     []comment{},
		AvgRating:    nil,
	}

	w.Header().Set("Location", "/recipes/"+recipeID)
	writeJSON(w, http.StatusCreated, response)
}

func (a *app) handleRecipeRoutes(w http.ResponseWriter, r *http.Request) {
	trimmed := strings.Trim(strings.TrimPrefix(r.URL.Path, "/recipes/"), "/")
	if trimmed == "" {
		http.NotFound(w, r)
		return
	}

	parts := strings.Split(trimmed, "/")
	recipeID := parts[0]
	if recipeID == "" {
		http.NotFound(w, r)
		return
	}

	if len(parts) == 1 {
		if r.Method != http.MethodGet {
			methodNotAllowed(w, http.MethodGet)
			return
		}
		a.handleRecipeDetail(w, r, recipeID)
		return
	}

	if len(parts) != 2 {
		http.NotFound(w, r)
		return
	}

	switch parts[1] {
	case "comments":
		if r.Method != http.MethodPost {
			methodNotAllowed(w, http.MethodPost)
			return
		}
		a.handleRecipeComment(w, r, recipeID)
	case "ratings":
		if r.Method != http.MethodPost {
			methodNotAllowed(w, http.MethodPost)
			return
		}
		a.handleRecipeRating(w, r, recipeID)
	default:
		http.NotFound(w, r)
	}
}

func (a *app) handleRecipeDetail(w http.ResponseWriter, r *http.Request, recipeID string) {
	now := time.Now()
	if body, ok := a.getRecipeCache(recipeID, now); ok {
		writeHTML(w, http.StatusOK, body)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	var data detailPageData
	var avgRating sql.NullFloat64
	if err := a.db.QueryRowContext(ctx, `
SELECT id, title, ingredients, instructions, avg_rating
FROM recipes
WHERE id = $1`, recipeID).Scan(&data.ID, &data.Title, pq.Array(&data.Ingredients), &data.Instructions, &avgRating); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			http.NotFound(w, r)
			return
		}
		serverError(w)
		return
	}

	comments, err := fetchComments(ctx, a.db, recipeID)
	if err != nil {
		serverError(w)
		return
	}
	data.Comments = comments
	if avgRating.Valid {
		data.HasRating = true
		data.AvgRating = fmt.Sprintf("%.2f / 5", avgRating.Float64)
	}

	body, err := renderHTML(detailTemplate, data)
	if err != nil {
		serverError(w)
		return
	}

	a.setRecipeCache(recipeID, body, detailCacheTTL)
	writeHTML(w, http.StatusOK, body)
}

func (a *app) handleRecipeComment(w http.ResponseWriter, r *http.Request, recipeID string) {
	var req commentRequest
	if err := decodeJSON(w, r, &req, commentBodyLimit); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	commentText := strings.TrimSpace(req.Comment)
	if commentText == "" {
		http.Error(w, "comment is required", http.StatusBadRequest)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	result, err := a.db.ExecContext(ctx, `
INSERT INTO comments (recipe_id, comment)
SELECT id, $2
FROM recipes
WHERE id = $1`, recipeID, commentText)
	if err != nil {
		serverError(w)
		return
	}

	rowsAffected, err := result.RowsAffected()
	if err != nil {
		serverError(w)
		return
	}
	if rowsAffected == 0 {
		http.NotFound(w, r)
		return
	}

	a.invalidateRecipe(recipeID)
	w.WriteHeader(http.StatusCreated)
}

func (a *app) handleRecipeRating(w http.ResponseWriter, r *http.Request, recipeID string) {
	var req ratingRequest
	if err := decodeJSON(w, r, &req, ratingBodyLimit); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if req.Rating < 1 || req.Rating > 5 {
		http.Error(w, "rating must be between 1 and 5", http.StatusBadRequest)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	result, err := a.db.ExecContext(ctx, `
UPDATE recipes
SET rating_sum = rating_sum + $1,
    rating_count = rating_count + 1,
    avg_rating = (rating_sum + $1)::double precision / (rating_count + 1)
WHERE id = $2`, req.Rating, recipeID)
	if err != nil {
		serverError(w)
		return
	}

	rowsAffected, err := result.RowsAffected()
	if err != nil {
		serverError(w)
		return
	}
	if rowsAffected == 0 {
		http.NotFound(w, r)
		return
	}

	a.invalidateRecipe(recipeID)
	a.overviewCache.invalidate()
	w.WriteHeader(http.StatusCreated)
}

func fetchRecipeLinks(ctx context.Context, db *sql.DB, query string, limit int) ([]recipeLink, error) {
	rows, err := db.QueryContext(ctx, query, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	links := make([]recipeLink, 0, limit)
	for rows.Next() {
		var link recipeLink
		if err := rows.Scan(&link.ID, &link.Title); err != nil {
			return nil, err
		}
		links = append(links, link)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	return links, nil
}

func fetchComments(ctx context.Context, db *sql.DB, recipeID string) ([]comment, error) {
	rows, err := db.QueryContext(ctx, `
SELECT comment
FROM comments
WHERE recipe_id = $1
ORDER BY created_at DESC`, recipeID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	comments := make([]comment, 0, 8)
	for rows.Next() {
		var item comment
		if err := rows.Scan(&item.Comment); err != nil {
			return nil, err
		}
		comments = append(comments, item)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	return comments, nil
}

func renderHTML(tpl *template.Template, data any) ([]byte, error) {
	var buf bytes.Buffer
	if err := tpl.Execute(&buf, data); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func decodeJSON(w http.ResponseWriter, r *http.Request, dst any, limit int64) error {
	contentType := r.Header.Get("Content-Type")
	if contentType != "" && !strings.HasPrefix(contentType, "application/json") {
		return errors.New("content type must be application/json")
	}

	r.Body = http.MaxBytesReader(w, r.Body, limit)
	defer r.Body.Close()

	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(dst); err != nil {
		return err
	}

	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		if err == nil {
			return errors.New("request body must contain a single JSON object")
		}
		return err
	}

	return nil
}

func sanitizeStrings(items []string) ([]string, bool) {
	if len(items) == 0 {
		return nil, false
	}

	cleaned := make([]string, 0, len(items))
	for _, item := range items {
		trimmed := strings.TrimSpace(item)
		if trimmed == "" {
			return nil, false
		}
		cleaned = append(cleaned, trimmed)
	}

	return cleaned, true
}

func writeHTML(w http.ResponseWriter, status int, body []byte) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func methodNotAllowed(w http.ResponseWriter, allowed ...string) {
	w.Header().Set("Allow", strings.Join(allowed, ", "))
	http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
}

func serverError(w http.ResponseWriter) {
	http.Error(w, "server error", http.StatusInternalServerError)
}

func firstNonEmpty(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}

func (c *htmlCache) get(now time.Time) ([]byte, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if len(c.body) == 0 || now.After(c.expires) {
		return nil, false
	}
	return c.body, true
}

func (c *htmlCache) set(body []byte, ttl time.Duration) {
	c.mu.Lock()
	c.body = append(c.body[:0], body...)
	c.expires = time.Now().Add(ttl)
	c.mu.Unlock()
}

func (c *htmlCache) invalidate() {
	c.mu.Lock()
	c.body = nil
	c.expires = time.Time{}
	c.mu.Unlock()
}

func (a *app) getRecipeCache(recipeID string, now time.Time) ([]byte, bool) {
	cached, ok := a.recipeCache.Load(recipeID)
	if !ok {
		return nil, false
	}

	page, ok := cached.(cachedPage)
	if !ok || now.UnixNano() >= page.expires {
		a.recipeCache.Delete(recipeID)
		return nil, false
	}

	return page.body, true
}

func (a *app) setRecipeCache(recipeID string, body []byte, ttl time.Duration) {
	a.recipeCache.Store(recipeID, cachedPage{
		body:    append([]byte(nil), body...),
		expires: time.Now().Add(ttl).UnixNano(),
	})
}

func (a *app) invalidateRecipe(recipeID string) {
	a.recipeCache.Delete(recipeID)
}
