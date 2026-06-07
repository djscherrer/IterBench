package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"io"
	"log"
	"net/http"
	"os"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/lib/pq"
)

const (
	defaultPort       = "5001"
	requestTimeout    = 3 * time.Second
	uploadBodyLimit   = 1 << 20
	commentBodyLimit  = 16 << 10
	ratingBodyLimit   = 4 << 10
	overviewCacheTTL  = 2 * time.Second
	maxTitleLength    = 200
	maxIngredientSize = 300
	maxIngredients    = 200
	maxCommentLength  = 2000
)

type App struct {
	db    *sql.DB
	cache overviewCache
}

type overviewCache struct {
	mu      sync.RWMutex
	body    string
	expires time.Time
}

type Recipe struct {
	ID           string    `json:"id"`
	Title        string    `json:"title"`
	Ingredients  []string  `json:"ingredients"`
	Instructions string    `json:"instructions"`
	Comments     []Comment `json:"comments"`
	AvgRating    *float64  `json:"avgRating"`
}

type Comment struct {
	Comment string `json:"comment"`
}

type uploadRecipeRequest struct {
	Title        string   `json:"title"`
	Ingredients  []string `json:"ingredients"`
	Instructions string   `json:"instructions"`
}

type addCommentRequest struct {
	Comment string `json:"comment"`
}

type addRatingRequest struct {
	Rating int `json:"rating"`
}

type overviewItem struct {
	ID        string
	Title     string
	AvgRating *float64
}

func main() {
	db, err := openDB()
	if err != nil {
		log.Fatalf("database setup failed: %v", err)
	}
	defer db.Close()

	app := &App{db: db}
	mux := http.NewServeMux()
	mux.HandleFunc("/recipes", app.handleRecipesOverview)
	mux.HandleFunc("/recipes/upload", app.handleRecipeUpload)
	mux.HandleFunc("/recipes/", app.handleRecipeRoutes)

	port := envOrDefault("PORT", defaultPort)
	server := &http.Server{
		Addr:              "0.0.0.0:" + port,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	log.Printf("recipe API listening on %s", server.Addr)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("server failed: %v", err)
	}
}

func openDB() (*sql.DB, error) {
	dsn := fmt.Sprintf(
		"host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		quoteConnInfo(os.Getenv("DB_HOST")),
		quoteConnInfo(envOrDefault("DB_PORT", "5432")),
		quoteConnInfo(os.Getenv("DB_USER")),
		quoteConnInfo(os.Getenv("DB_PASSWORD")),
		quoteConnInfo(os.Getenv("DB_NAME")),
	)

	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}

	maxOpen := runtime.NumCPU() * 8
	if maxOpen < 32 {
		maxOpen = 32
	}
	if maxOpen > 128 {
		maxOpen = 128
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
	if err := initSchema(ctx, db); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

func quoteConnInfo(value string) string {
	value = strings.ReplaceAll(value, `\`, `\\`)
	value = strings.ReplaceAll(value, `'`, `\'`)
	return "'" + value + "'"
}

func initSchema(ctx context.Context, db *sql.DB) error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS recipes (
			id TEXT PRIMARY KEY,
			title TEXT NOT NULL CHECK (length(title) > 0),
			ingredients TEXT[] NOT NULL CHECK (array_length(ingredients, 1) > 0),
			instructions TEXT NOT NULL CHECK (length(instructions) > 0),
			rating_sum BIGINT NOT NULL DEFAULT 0 CHECK (rating_sum >= 0),
			rating_count INTEGER NOT NULL DEFAULT 0 CHECK (rating_count >= 0),
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)`,
		`CREATE TABLE IF NOT EXISTS comments (
			id BIGSERIAL PRIMARY KEY,
			recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
			comment TEXT NOT NULL CHECK (length(comment) > 0),
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)`,
		`CREATE TABLE IF NOT EXISTS ratings (
			id BIGSERIAL PRIMARY KEY,
			recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
			rating SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)`,
		`CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes (created_at DESC)`,
		`CREATE INDEX IF NOT EXISTS idx_recipes_rating ON recipes (rating_count DESC, rating_sum DESC) WHERE rating_count > 0`,
		`CREATE INDEX IF NOT EXISTS idx_comments_recipe_created ON comments (recipe_id, created_at ASC)`,
		`CREATE INDEX IF NOT EXISTS idx_ratings_recipe_created ON ratings (recipe_id, created_at DESC)`,
	}

	for _, stmt := range statements {
		if _, err := db.ExecContext(ctx, stmt); err != nil {
			return fmt.Errorf("executing schema statement: %w", err)
		}
	}
	return nil
}

func (app *App) handleRecipesOverview(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/recipes" {
		notFound(w)
		return
	}
	if r.Method != http.MethodGet {
		methodNotAllowed(w, http.MethodGet)
		return
	}

	if body, ok := app.cache.get(); ok {
		writeHTML(w, http.StatusOK, body)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), requestTimeout)
	defer cancel()

	recent, err := app.fetchOverviewItems(ctx, `
		SELECT id, title, CASE WHEN rating_count = 0 THEN NULL ELSE rating_sum::double precision / rating_count END
		FROM recipes
		ORDER BY created_at DESC
		LIMIT 50`)
	if err != nil {
		serverError(w, err)
		return
	}
	topRated, err := app.fetchOverviewItems(ctx, `
		SELECT id, title, rating_sum::double precision / rating_count
		FROM recipes
		WHERE rating_count > 0
		ORDER BY rating_sum::double precision / rating_count DESC, rating_count DESC, created_at DESC
		LIMIT 20`)
	if err != nil {
		serverError(w, err)
		return
	}

	body := buildOverviewHTML(recent, topRated)
	app.cache.set(body)
	writeHTML(w, http.StatusOK, body)
}

func (app *App) fetchOverviewItems(ctx context.Context, query string) ([]overviewItem, error) {
	rows, err := app.db.QueryContext(ctx, query)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	items := make([]overviewItem, 0, 50)
	for rows.Next() {
		var item overviewItem
		var avg sql.NullFloat64
		if err := rows.Scan(&item.ID, &item.Title, &avg); err != nil {
			return nil, err
		}
		if avg.Valid {
			value := avg.Float64
			item.AvgRating = &value
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func (app *App) handleRecipeUpload(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}

	var req uploadRecipeRequest
	if !decodeJSON(w, r, &req, uploadBodyLimit) {
		return
	}
	if err := normalizeUpload(&req); err != nil {
		clientError(w, http.StatusBadRequest, err.Error())
		return
	}

	recipe := Recipe{
		ID:           uuid.NewString(),
		Title:        req.Title,
		Ingredients:  req.Ingredients,
		Instructions: req.Instructions,
		Comments:     []Comment{},
	}

	ctx, cancel := context.WithTimeout(r.Context(), requestTimeout)
	defer cancel()
	_, err := app.db.ExecContext(ctx,
		`INSERT INTO recipes (id, title, ingredients, instructions) VALUES ($1, $2, $3, $4)`,
		recipe.ID, recipe.Title, pq.Array(recipe.Ingredients), recipe.Instructions,
	)
	if err != nil {
		serverError(w, err)
		return
	}
	app.cache.invalidate()
	writeJSON(w, http.StatusCreated, recipe)
}

func (app *App) handleRecipeRoutes(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/recipes/")
	parts := strings.Split(path, "/")
	if len(parts) == 1 && parts[0] != "" {
		if r.Method != http.MethodGet {
			methodNotAllowed(w, http.MethodGet)
			return
		}
		app.handleRecipeGet(w, r, parts[0])
		return
	}
	if len(parts) == 2 && parts[0] != "" && parts[1] == "comments" {
		if r.Method != http.MethodPost {
			methodNotAllowed(w, http.MethodPost)
			return
		}
		app.handleCommentCreate(w, r, parts[0])
		return
	}
	if len(parts) == 2 && parts[0] != "" && parts[1] == "ratings" {
		if r.Method != http.MethodPost {
			methodNotAllowed(w, http.MethodPost)
			return
		}
		app.handleRatingCreate(w, r, parts[0])
		return
	}
	notFound(w)
}

func (app *App) handleRecipeGet(w http.ResponseWriter, r *http.Request, recipeID string) {
	ctx, cancel := context.WithTimeout(r.Context(), requestTimeout)
	defer cancel()

	recipe, err := app.loadRecipe(ctx, recipeID)
	if errors.Is(err, sql.ErrNoRows) {
		notFound(w)
		return
	}
	if err != nil {
		serverError(w, err)
		return
	}
	comments, err := app.loadComments(ctx, recipeID)
	if err != nil {
		serverError(w, err)
		return
	}
	recipe.Comments = comments
	writeHTML(w, http.StatusOK, buildRecipeHTML(recipe))
}

func (app *App) handleCommentCreate(w http.ResponseWriter, r *http.Request, recipeID string) {
	var req addCommentRequest
	if !decodeJSON(w, r, &req, commentBodyLimit) {
		return
	}
	req.Comment = strings.TrimSpace(req.Comment)
	if req.Comment == "" || len(req.Comment) > maxCommentLength {
		clientError(w, http.StatusBadRequest, "comment is required and must not exceed 2000 characters")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), requestTimeout)
	defer cancel()
	tx, err := app.db.BeginTx(ctx, nil)
	if err != nil {
		serverError(w, err)
		return
	}
	defer tx.Rollback()

	var exists bool
	if err := tx.QueryRowContext(ctx, `SELECT EXISTS (SELECT 1 FROM recipes WHERE id = $1)`, recipeID).Scan(&exists); err != nil {
		serverError(w, err)
		return
	}
	if !exists {
		notFound(w)
		return
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO comments (recipe_id, comment) VALUES ($1, $2)`, recipeID, req.Comment); err != nil {
		serverError(w, err)
		return
	}
	if err := tx.Commit(); err != nil {
		serverError(w, err)
		return
	}
	w.WriteHeader(http.StatusCreated)
}

func (app *App) handleRatingCreate(w http.ResponseWriter, r *http.Request, recipeID string) {
	var req addRatingRequest
	if !decodeJSON(w, r, &req, ratingBodyLimit) {
		return
	}
	if req.Rating < 1 || req.Rating > 5 {
		clientError(w, http.StatusBadRequest, "rating must be an integer from 1 to 5")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), requestTimeout)
	defer cancel()
	tx, err := app.db.BeginTx(ctx, nil)
	if err != nil {
		serverError(w, err)
		return
	}
	defer tx.Rollback()

	result, err := tx.ExecContext(ctx,
		`UPDATE recipes SET rating_sum = rating_sum + $2, rating_count = rating_count + 1 WHERE id = $1`,
		recipeID, req.Rating,
	)
	if err != nil {
		serverError(w, err)
		return
	}
	affected, err := result.RowsAffected()
	if err != nil {
		serverError(w, err)
		return
	}
	if affected == 0 {
		notFound(w)
		return
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO ratings (recipe_id, rating) VALUES ($1, $2)`, recipeID, req.Rating); err != nil {
		serverError(w, err)
		return
	}
	if err := tx.Commit(); err != nil {
		serverError(w, err)
		return
	}
	app.cache.invalidate()
	w.WriteHeader(http.StatusCreated)
}

func (app *App) loadRecipe(ctx context.Context, recipeID string) (Recipe, error) {
	var recipe Recipe
	var ingredients pq.StringArray
	var avg sql.NullFloat64
	err := app.db.QueryRowContext(ctx, `
		SELECT id, title, ingredients, instructions,
		       CASE WHEN rating_count = 0 THEN NULL ELSE rating_sum::double precision / rating_count END
		FROM recipes
		WHERE id = $1`, recipeID).Scan(&recipe.ID, &recipe.Title, &ingredients, &recipe.Instructions, &avg)
	if err != nil {
		return Recipe{}, err
	}
	recipe.Ingredients = []string(ingredients)
	if avg.Valid {
		value := avg.Float64
		recipe.AvgRating = &value
	}
	return recipe, nil
}

func (app *App) loadComments(ctx context.Context, recipeID string) ([]Comment, error) {
	rows, err := app.db.QueryContext(ctx, `SELECT comment FROM comments WHERE recipe_id = $1 ORDER BY created_at ASC`, recipeID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	comments := make([]Comment, 0)
	for rows.Next() {
		var comment Comment
		if err := rows.Scan(&comment.Comment); err != nil {
			return nil, err
		}
		comments = append(comments, comment)
	}
	return comments, rows.Err()
}

func normalizeUpload(req *uploadRecipeRequest) error {
	req.Title = strings.TrimSpace(req.Title)
	req.Instructions = strings.TrimSpace(req.Instructions)
	if req.Title == "" || len(req.Title) > maxTitleLength {
		return fmt.Errorf("title is required and must not exceed %d characters", maxTitleLength)
	}
	if req.Instructions == "" {
		return fmt.Errorf("instructions are required")
	}
	if len(req.Ingredients) == 0 || len(req.Ingredients) > maxIngredients {
		return fmt.Errorf("ingredients must contain between 1 and %d items", maxIngredients)
	}
	for i, ingredient := range req.Ingredients {
		ingredient = strings.TrimSpace(ingredient)
		if ingredient == "" || len(ingredient) > maxIngredientSize {
			return fmt.Errorf("each ingredient is required and must not exceed %d characters", maxIngredientSize)
		}
		req.Ingredients[i] = ingredient
	}
	return nil
}

func decodeJSON(w http.ResponseWriter, r *http.Request, dst any, limit int64) bool {
	r.Body = http.MaxBytesReader(w, r.Body, limit)
	defer r.Body.Close()

	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(dst); err != nil {
		clientError(w, http.StatusBadRequest, "invalid JSON request body")
		return false
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		clientError(w, http.StatusBadRequest, "request body must contain a single JSON object")
		return false
	}
	return true
}

func buildOverviewHTML(recent, topRated []overviewItem) string {
	var b strings.Builder
	b.Grow(4096)
	b.WriteString("<!doctype html><html><head><meta charset=\"utf-8\"><title>Recipes</title></head><body>")
	b.WriteString("<h1>Recipes</h1><h2>Recent recipes</h2>")
	writeOverviewList(&b, recent)
	b.WriteString("<h2>Top-rated recipes</h2>")
	writeOverviewList(&b, topRated)
	b.WriteString("</body></html>")
	return b.String()
}

func writeOverviewList(b *strings.Builder, items []overviewItem) {
	if len(items) == 0 {
		b.WriteString("<p>No recipes yet.</p>")
		return
	}
	b.WriteString("<ul>")
	for _, item := range items {
		id := html.EscapeString(item.ID)
		b.WriteString("<li><a href=\"/recipes/")
		b.WriteString(id)
		b.WriteString("\">")
		b.WriteString(html.EscapeString(item.Title))
		b.WriteString("</a> <span>Rating: ")
		b.WriteString(formatAvg(item.AvgRating))
		b.WriteString("</span></li>")
	}
	b.WriteString("</ul>")
}

func buildRecipeHTML(recipe Recipe) string {
	var b strings.Builder
	b.Grow(4096 + len(recipe.Instructions) + len(recipe.Comments)*80)
	b.WriteString("<!doctype html><html><head><meta charset=\"utf-8\"><title>")
	b.WriteString(html.EscapeString(recipe.Title))
	b.WriteString("</title></head><body><article><h1>")
	b.WriteString(html.EscapeString(recipe.Title))
	b.WriteString("</h1><p><strong>Average rating:</strong> ")
	b.WriteString(formatAvg(recipe.AvgRating))
	b.WriteString("</p><h2>Ingredients</h2><ul>")
	for _, ingredient := range recipe.Ingredients {
		b.WriteString("<li>")
		b.WriteString(html.EscapeString(ingredient))
		b.WriteString("</li>")
	}
	b.WriteString("</ul><h2>Instructions</h2><p>")
	b.WriteString(html.EscapeString(recipe.Instructions))
	b.WriteString("</p><h2>Comments</h2>")
	if len(recipe.Comments) == 0 {
		b.WriteString("<p>No comments yet.</p>")
	} else {
		b.WriteString("<ul>")
		for _, comment := range recipe.Comments {
			b.WriteString("<li>")
			b.WriteString(html.EscapeString(comment.Comment))
			b.WriteString("</li>")
		}
		b.WriteString("</ul>")
	}
	b.WriteString("</article></body></html>")
	return b.String()
}

func formatAvg(avg *float64) string {
	if avg == nil {
		return "N/A"
	}
	return strconv.FormatFloat(*avg, 'f', 2, 64)
}

func writeHTML(w http.ResponseWriter, status int, body string) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(status)
	_, _ = w.Write([]byte(body))
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func clientError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}

func serverError(w http.ResponseWriter, err error) {
	log.Printf("request failed: %v", err)
	clientError(w, http.StatusInternalServerError, "server error")
}

func notFound(w http.ResponseWriter) {
	clientError(w, http.StatusNotFound, "not found")
}

func methodNotAllowed(w http.ResponseWriter, allowed string) {
	w.Header().Set("Allow", allowed)
	clientError(w, http.StatusMethodNotAllowed, "method not allowed")
}

func (c *overviewCache) get() (string, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if c.body == "" || time.Now().After(c.expires) {
		return "", false
	}
	return c.body, true
}

func (c *overviewCache) set(body string) {
	c.mu.Lock()
	c.body = body
	c.expires = time.Now().Add(overviewCacheTTL)
	c.mu.Unlock()
}

func (c *overviewCache) invalidate() {
	c.mu.Lock()
	c.body = ""
	c.expires = time.Time{}
	c.mu.Unlock()
}

func envOrDefault(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}
