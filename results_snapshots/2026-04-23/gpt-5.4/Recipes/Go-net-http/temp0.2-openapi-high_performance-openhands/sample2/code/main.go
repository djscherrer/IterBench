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
	"os"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

const (
	overviewCacheKey = "overview"
	jsonBodyLimit    = 1 << 20
)

const schemaSQL = `
CREATE TABLE IF NOT EXISTS recipes (
    id UUID PRIMARY KEY,
    title TEXT NOT NULL CHECK (length(title) > 0),
    ingredients JSONB NOT NULL CHECK (jsonb_typeof(ingredients) = 'array'),
    instructions TEXT NOT NULL CHECK (length(instructions) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS recipe_comments (
    id BIGSERIAL PRIMARY KEY,
    recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    comment TEXT NOT NULL CHECK (length(comment) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS recipe_ratings (
    id BIGSERIAL PRIMARY KEY,
    recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    rating SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS recipe_stats (
    recipe_id UUID PRIMARY KEY REFERENCES recipes(id) ON DELETE CASCADE,
    comment_count BIGINT NOT NULL DEFAULT 0,
    rating_count BIGINT NOT NULL DEFAULT 0,
    rating_sum BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_recipe_comments_recipe_created ON recipe_comments (recipe_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_recipe_ratings_recipe_created ON recipe_ratings (recipe_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_recipe_stats_rating_lookup ON recipe_stats (rating_count DESC, rating_sum DESC);

INSERT INTO recipe_stats (recipe_id, comment_count, rating_count, rating_sum, updated_at)
SELECT
    r.id,
    COALESCE(c.comment_count, 0),
    COALESCE(rt.rating_count, 0),
    COALESCE(rt.rating_sum, 0),
    NOW()
FROM recipes r
LEFT JOIN (
    SELECT recipe_id, COUNT(*)::BIGINT AS comment_count
    FROM recipe_comments
    GROUP BY recipe_id
) c ON c.recipe_id = r.id
LEFT JOIN (
    SELECT recipe_id, COUNT(*)::BIGINT AS rating_count, COALESCE(SUM(rating), 0)::BIGINT AS rating_sum
    FROM recipe_ratings
    GROUP BY recipe_id
) rt ON rt.recipe_id = r.id
ON CONFLICT (recipe_id) DO UPDATE
SET
    comment_count = EXCLUDED.comment_count,
    rating_count = EXCLUDED.rating_count,
    rating_sum = EXCLUDED.rating_sum,
    updated_at = EXCLUDED.updated_at;
`

const recentRecipesSQL = `
SELECT
    r.id,
    r.title,
    CASE
        WHEN s.rating_count = 0 THEN NULL
        ELSE ROUND((s.rating_sum::numeric / s.rating_count), 2)::double precision
    END AS avg_rating
FROM recipes r
JOIN recipe_stats s ON s.recipe_id = r.id
ORDER BY r.created_at DESC
LIMIT 10;
`

const topRecipesSQL = `
SELECT
    r.id,
    r.title,
    CASE
        WHEN s.rating_count = 0 THEN NULL
        ELSE ROUND((s.rating_sum::numeric / s.rating_count), 2)::double precision
    END AS avg_rating
FROM recipes r
JOIN recipe_stats s ON s.recipe_id = r.id
ORDER BY
    CASE
        WHEN s.rating_count = 0 THEN NULL
        ELSE (s.rating_sum::numeric / s.rating_count)
    END DESC NULLS LAST,
    s.rating_count DESC,
    r.created_at DESC
LIMIT 10;
`

const recipeDetailSQL = `
SELECT
    r.id,
    r.title,
    r.ingredients,
    r.instructions,
    CASE
        WHEN s.rating_count = 0 THEN NULL
        ELSE ROUND((s.rating_sum::numeric / s.rating_count), 2)::double precision
    END AS avg_rating,
    s.rating_count,
    s.comment_count
FROM recipes r
JOIN recipe_stats s ON s.recipe_id = r.id
WHERE r.id = $1;
`

const recipeCommentsSQL = `
SELECT comment
FROM recipe_comments
WHERE recipe_id = $1
ORDER BY created_at ASC, id ASC;
`

const insertCommentSQL = `
WITH inserted_comment AS (
    INSERT INTO recipe_comments (recipe_id, comment)
    SELECT id, $2
    FROM recipes
    WHERE id = $1
    RETURNING recipe_id
), updated_stats AS (
    UPDATE recipe_stats
    SET comment_count = comment_count + 1,
        updated_at = NOW()
    WHERE recipe_id IN (SELECT recipe_id FROM inserted_comment)
    RETURNING recipe_id
)
SELECT recipe_id FROM updated_stats;
`

const insertRatingSQL = `
WITH inserted_rating AS (
    INSERT INTO recipe_ratings (recipe_id, rating)
    SELECT id, $2
    FROM recipes
    WHERE id = $1
    RETURNING recipe_id
), updated_stats AS (
    UPDATE recipe_stats
    SET rating_count = rating_count + 1,
        rating_sum = rating_sum + $2,
        updated_at = NOW()
    WHERE recipe_id IN (SELECT recipe_id FROM inserted_rating)
    RETURNING recipe_id
)
SELECT recipe_id FROM updated_stats;
`

const overviewPageHTML = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Recipes</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 2rem auto; max-width: 960px; line-height: 1.5; color: #222; }
    h1, h2 { color: #111; }
    .grid { display: grid; gap: 1.5rem; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
    .card { border: 1px solid #ddd; border-radius: 8px; padding: 1rem; background: #fff; }
    ul { padding-left: 1.25rem; }
    a { color: #0b57d0; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .muted { color: #666; font-size: 0.95rem; }
  </style>
</head>
<body>
  <h1>Recipe Overview</h1>
  <div class="grid">
    <section class="card">
      <h2>Recent Recipes</h2>
      {{if .RecentRecipes}}
      <ul>
        {{range .RecentRecipes}}
        <li>
          <a href="/recipes/{{.ID}}">{{.Title}}</a>
          <div class="muted">Average rating: {{formatRating .AvgRating}}</div>
        </li>
        {{end}}
      </ul>
      {{else}}
      <p>No recipes have been uploaded yet.</p>
      {{end}}
    </section>
    <section class="card">
      <h2>Top Rated Recipes</h2>
      {{if .TopRecipes}}
      <ul>
        {{range .TopRecipes}}
        <li>
          <a href="/recipes/{{.ID}}">{{.Title}}</a>
          <div class="muted">Average rating: {{formatRating .AvgRating}}</div>
        </li>
        {{end}}
      </ul>
      {{else}}
      <p>No ratings have been submitted yet.</p>
      {{end}}
    </section>
  </div>
</body>
</html>
`

const recipePageHTML = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{.Recipe.Title}}</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 2rem auto; max-width: 900px; line-height: 1.5; color: #222; }
    h1, h2 { color: #111; }
    .meta { color: #666; margin-bottom: 1rem; }
    .instructions { white-space: pre-wrap; border: 1px solid #ddd; border-radius: 8px; padding: 1rem; background: #fafafa; }
    .card { border: 1px solid #ddd; border-radius: 8px; padding: 1rem; background: #fff; margin-top: 1.5rem; }
    ul { padding-left: 1.25rem; }
    a { color: #0b57d0; text-decoration: none; }
  </style>
</head>
<body>
  <p><a href="/recipes">&larr; Back to overview</a></p>
  <h1>{{.Recipe.Title}}</h1>
  <p class="meta">Average rating: {{formatRating .Recipe.AvgRating}} · Ratings: {{.RatingCount}} · Comments: {{.CommentCount}}</p>

  <section class="card">
    <h2>Ingredients</h2>
    <ul>
      {{range .Recipe.Ingredients}}
      <li>{{.}}</li>
      {{end}}
    </ul>
  </section>

  <section class="card">
    <h2>Instructions</h2>
    <div class="instructions">{{.Recipe.Instructions}}</div>
  </section>

  <section class="card">
    <h2>Comments</h2>
    {{if .Recipe.Comments}}
    <ul>
      {{range .Recipe.Comments}}
      <li>{{.Comment}}</li>
      {{end}}
    </ul>
    {{else}}
    <p>No comments yet.</p>
    {{end}}
  </section>
</body>
</html>
`

type app struct {
	db           *sql.DB
	cache        *htmlCache
	overviewTmpl *template.Template
	recipeTmpl   *template.Template
}

type htmlCache struct {
	mu    sync.RWMutex
	ttl   time.Duration
	items map[string]cacheEntry
}

type cacheEntry struct {
	body      []byte
	expiresAt time.Time
}

type recipe struct {
	ID           string          `json:"id"`
	Title        string          `json:"title"`
	Ingredients  []string        `json:"ingredients"`
	Instructions string          `json:"instructions"`
	Comments     []recipeComment `json:"comments"`
	AvgRating    *float64        `json:"avgRating"`
}

type recipeComment struct {
	Comment string `json:"comment"`
}

type uploadRecipeRequest struct {
	Title        string   `json:"title"`
	Ingredients  []string `json:"ingredients"`
	Instructions string   `json:"instructions"`
}

type createCommentRequest struct {
	Comment string `json:"comment"`
}

type createRatingRequest struct {
	Rating int `json:"rating"`
}

type recipeSummary struct {
	ID        string
	Title     string
	AvgRating *float64
}

type overviewPageData struct {
	RecentRecipes []recipeSummary
	TopRecipes    []recipeSummary
}

type recipePageData struct {
	Recipe       recipe
	RatingCount  int64
	CommentCount int64
}

func main() {
	log.SetFlags(log.LstdFlags | log.LUTC | log.Lshortfile)

	port := strings.TrimSpace(os.Getenv("PORT"))
	if port == "" {
		port = "5001"
	}

	db, err := openDBFromEnv()
	if err != nil {
		log.Fatalf("open database: %v", err)
	}
	defer db.Close()

	startupCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	if err := db.PingContext(startupCtx); err != nil {
		log.Fatalf("ping database: %v", err)
	}

	appInstance, err := newApp(startupCtx, db)
	if err != nil {
		log.Fatalf("initialize app: %v", err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /recipes", appInstance.handleRecipesOverview)
	mux.HandleFunc("POST /recipes/upload", appInstance.handleUploadRecipe)
	mux.HandleFunc("GET /recipes/{recipeId}", appInstance.handleGetRecipe)
	mux.HandleFunc("POST /recipes/{recipeId}/comments", appInstance.handleCreateComment)
	mux.HandleFunc("POST /recipes/{recipeId}/ratings", appInstance.handleCreateRating)

	server := &http.Server{
		Addr:              net.JoinHostPort("0.0.0.0", port),
		Handler:           mux,
		ReadHeaderTimeout: 2 * time.Second,
		ReadTimeout:       5 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       120 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}

	log.Printf("listening on %s", server.Addr)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("server stopped: %v", err)
	}
}

func openDBFromEnv() (*sql.DB, error) {
	host := requiredEnv("DB_HOST")
	port := envOrDefault("DB_PORT", "5432")
	user := requiredEnv("DB_USER")
	password := requiredEnv("DB_PASSWORD")
	database := requiredEnv("DB_NAME")

	dsn := fmt.Sprintf(
		"host=%s port=%s user=%s password=%s dbname=%s sslmode=disable connect_timeout=5",
		host,
		port,
		user,
		password,
		database,
	)

	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}

	configureDBPool(db)
	return db, nil
}

func configureDBPool(db *sql.DB) {
	maxOpen := runtime.GOMAXPROCS(0) * 16
	if maxOpen < 32 {
		maxOpen = 32
	}
	if maxOpen > 256 {
		maxOpen = 256
	}

	maxIdle := maxOpen / 2
	if maxIdle < 16 {
		maxIdle = 16
	}

	db.SetMaxOpenConns(maxOpen)
	db.SetMaxIdleConns(maxIdle)
	db.SetConnMaxIdleTime(5 * time.Minute)
	db.SetConnMaxLifetime(30 * time.Minute)
}

func newApp(ctx context.Context, db *sql.DB) (*app, error) {
	if _, err := db.ExecContext(ctx, schemaSQL); err != nil {
		return nil, err
	}

	funcMap := template.FuncMap{
		"formatRating": formatRating,
	}

	return &app{
		db:           db,
		cache:        newHTMLCache(3 * time.Second),
		overviewTmpl: template.Must(template.New("overview").Funcs(funcMap).Parse(overviewPageHTML)),
		recipeTmpl:   template.Must(template.New("recipe").Funcs(funcMap).Parse(recipePageHTML)),
	}, nil
}

func newHTMLCache(ttl time.Duration) *htmlCache {
	return &htmlCache{
		ttl:   ttl,
		items: make(map[string]cacheEntry),
	}
}

func (c *htmlCache) get(key string) ([]byte, bool) {
	now := time.Now()

	c.mu.RLock()
	entry, ok := c.items[key]
	c.mu.RUnlock()
	if !ok {
		return nil, false
	}
	if now.After(entry.expiresAt) {
		c.mu.Lock()
		if current, found := c.items[key]; found && now.After(current.expiresAt) {
			delete(c.items, key)
		}
		c.mu.Unlock()
		return nil, false
	}

	return entry.body, true
}

func (c *htmlCache) set(key string, body []byte) {
	copied := append([]byte(nil), body...)

	c.mu.Lock()
	c.items[key] = cacheEntry{
		body:      copied,
		expiresAt: time.Now().Add(c.ttl),
	}
	c.mu.Unlock()
}

func (c *htmlCache) invalidate(keys ...string) {
	c.mu.Lock()
	if len(keys) == 0 {
		clear(c.items)
	} else {
		for _, key := range keys {
			delete(c.items, key)
		}
	}
	c.mu.Unlock()
}

func (a *app) handleRecipesOverview(w http.ResponseWriter, r *http.Request) {
	if body, ok := a.cache.get(overviewCacheKey); ok {
		writeHTML(w, body)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	recentRecipes, err := a.listRecipeSummaries(ctx, recentRecipesSQL)
	if err != nil {
		log.Printf("load recent recipes: %v", err)
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	topRecipes, err := a.listRecipeSummaries(ctx, topRecipesSQL)
	if err != nil {
		log.Printf("load top recipes: %v", err)
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	var buf bytes.Buffer
	if err := a.overviewTmpl.Execute(&buf, overviewPageData{
		RecentRecipes: recentRecipes,
		TopRecipes:    topRecipes,
	}); err != nil {
		log.Printf("render overview: %v", err)
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	body := buf.Bytes()
	a.cache.set(overviewCacheKey, body)
	writeHTML(w, body)
}

func (a *app) listRecipeSummaries(ctx context.Context, query string) ([]recipeSummary, error) {
	rows, err := a.db.QueryContext(ctx, query)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	summaries := make([]recipeSummary, 0, 10)
	for rows.Next() {
		var item recipeSummary
		var avg sql.NullFloat64
		if err := rows.Scan(&item.ID, &item.Title, &avg); err != nil {
			return nil, err
		}
		if avg.Valid {
			value := avg.Float64
			item.AvgRating = &value
		}
		summaries = append(summaries, item)
	}

	if err := rows.Err(); err != nil {
		return nil, err
	}
	return summaries, nil
}

func (a *app) handleUploadRecipe(w http.ResponseWriter, r *http.Request) {
	var req uploadRecipeRequest
	if err := decodeJSONBody(w, r, &req); err != nil {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}

	title := strings.TrimSpace(req.Title)
	instructions := strings.TrimSpace(req.Instructions)
	if title == "" || instructions == "" || len(req.Ingredients) == 0 {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}

	ingredients := make([]string, 0, len(req.Ingredients))
	for _, ingredient := range req.Ingredients {
		trimmed := strings.TrimSpace(ingredient)
		if trimmed == "" {
			http.Error(w, "invalid input", http.StatusBadRequest)
			return
		}
		ingredients = append(ingredients, trimmed)
	}

	ingredientsJSON, err := json.Marshal(ingredients)
	if err != nil {
		log.Printf("marshal ingredients: %v", err)
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	tx, err := a.db.BeginTx(ctx, nil)
	if err != nil {
		log.Printf("begin upload tx: %v", err)
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}
	defer tx.Rollback()

	newID := uuid.NewString()
	if _, err := tx.ExecContext(
		ctx,
		`INSERT INTO recipes (id, title, ingredients, instructions) VALUES ($1, $2, $3::jsonb, $4);`,
		newID,
		title,
		string(ingredientsJSON),
		instructions,
	); err != nil {
		log.Printf("insert recipe: %v", err)
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	if _, err := tx.ExecContext(ctx, `INSERT INTO recipe_stats (recipe_id) VALUES ($1);`, newID); err != nil {
		log.Printf("insert recipe stats: %v", err)
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	if err := tx.Commit(); err != nil {
		log.Printf("commit upload tx: %v", err)
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	a.cache.invalidate(overviewCacheKey)
	writeJSON(w, http.StatusCreated, recipe{
		ID:           newID,
		Title:        title,
		Ingredients:  ingredients,
		Instructions: instructions,
		Comments:     make([]recipeComment, 0),
		AvgRating:    nil,
	})
}

func (a *app) handleGetRecipe(w http.ResponseWriter, r *http.Request) {
	recipeID, ok := parseRecipeID(r.PathValue("recipeId"))
	if !ok {
		http.NotFound(w, r)
		return
	}

	cacheKey := recipeCacheKey(recipeID)
	if body, ok := a.cache.get(cacheKey); ok {
		writeHTML(w, body)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	recipeData, ratingCount, commentCount, err := a.getRecipe(ctx, recipeID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			http.NotFound(w, r)
			return
		}
		log.Printf("load recipe %s: %v", recipeID, err)
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	comments, err := a.getRecipeComments(ctx, recipeID)
	if err != nil {
		log.Printf("load recipe comments %s: %v", recipeID, err)
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}
	recipeData.Comments = comments

	var buf bytes.Buffer
	if err := a.recipeTmpl.Execute(&buf, recipePageData{
		Recipe:       recipeData,
		RatingCount:  ratingCount,
		CommentCount: commentCount,
	}); err != nil {
		log.Printf("render recipe page %s: %v", recipeID, err)
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	body := buf.Bytes()
	a.cache.set(cacheKey, body)
	writeHTML(w, body)
}

func (a *app) getRecipe(ctx context.Context, recipeID string) (recipe, int64, int64, error) {
	var item recipe
	var ingredientsJSON []byte
	var avg sql.NullFloat64
	var ratingCount int64
	var commentCount int64

	err := a.db.QueryRowContext(ctx, recipeDetailSQL, recipeID).Scan(
		&item.ID,
		&item.Title,
		&ingredientsJSON,
		&item.Instructions,
		&avg,
		&ratingCount,
		&commentCount,
	)
	if err != nil {
		return recipe{}, 0, 0, err
	}

	if err := json.Unmarshal(ingredientsJSON, &item.Ingredients); err != nil {
		return recipe{}, 0, 0, err
	}
	if avg.Valid {
		value := avg.Float64
		item.AvgRating = &value
	}

	return item, ratingCount, commentCount, nil
}

func (a *app) getRecipeComments(ctx context.Context, recipeID string) ([]recipeComment, error) {
	rows, err := a.db.QueryContext(ctx, recipeCommentsSQL, recipeID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	comments := make([]recipeComment, 0, 8)
	for rows.Next() {
		var item recipeComment
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

func (a *app) handleCreateComment(w http.ResponseWriter, r *http.Request) {
	recipeID, ok := parseRecipeID(r.PathValue("recipeId"))
	if !ok {
		http.NotFound(w, r)
		return
	}

	var req createCommentRequest
	if err := decodeJSONBody(w, r, &req); err != nil {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}

	comment := strings.TrimSpace(req.Comment)
	if comment == "" {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	var updatedRecipeID string
	err := a.db.QueryRowContext(ctx, insertCommentSQL, recipeID, comment).Scan(&updatedRecipeID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			http.NotFound(w, r)
			return
		}
		log.Printf("insert comment for recipe %s: %v", recipeID, err)
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	a.cache.invalidate(recipeCacheKey(recipeID))
	w.WriteHeader(http.StatusCreated)
}

func (a *app) handleCreateRating(w http.ResponseWriter, r *http.Request) {
	recipeID, ok := parseRecipeID(r.PathValue("recipeId"))
	if !ok {
		http.NotFound(w, r)
		return
	}

	var req createRatingRequest
	if err := decodeJSONBody(w, r, &req); err != nil {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}
	if req.Rating < 1 || req.Rating > 5 {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	var updatedRecipeID string
	err := a.db.QueryRowContext(ctx, insertRatingSQL, recipeID, req.Rating).Scan(&updatedRecipeID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			http.NotFound(w, r)
			return
		}
		log.Printf("insert rating for recipe %s: %v", recipeID, err)
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	a.cache.invalidate(overviewCacheKey, recipeCacheKey(recipeID))
	w.WriteHeader(http.StatusCreated)
}

func parseRecipeID(value string) (string, bool) {
	parsed, err := uuid.Parse(strings.TrimSpace(value))
	if err != nil {
		return "", false
	}
	return parsed.String(), true
}

func decodeJSONBody(w http.ResponseWriter, r *http.Request, dst any) error {
	r.Body = http.MaxBytesReader(w, r.Body, jsonBodyLimit)
	defer r.Body.Close()

	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()

	if err := decoder.Decode(dst); err != nil {
		return err
	}

	var trailing json.RawMessage
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			return errors.New("request body must contain a single JSON object")
		}
		return err
	}

	return nil
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(value); err != nil {
		log.Printf("write json response: %v", err)
	}
}

func writeHTML(w http.ResponseWriter, body []byte) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write(body)
}

func recipeCacheKey(recipeID string) string {
	return "recipe:" + recipeID
}

func formatRating(value *float64) string {
	if value == nil {
		return "Not rated yet"
	}
	return strconv.FormatFloat(*value, 'f', 2, 64)
}

func requiredEnv(key string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		log.Fatalf("%s is required", key)
	}
	return value
}

func envOrDefault(key, fallback string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	return value
}
