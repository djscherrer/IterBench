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
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/lib/pq"
)

const (
	overviewLimit      = 20
	uploadBodyLimit    = 1 << 20
	smallBodyLimit     = 1 << 16
	dbTimeout          = 5 * time.Second
	serverReadTimeout  = 5 * time.Second
	serverWriteTimeout = 10 * time.Second
)

type app struct {
	logger *log.Logger
	db     *sql.DB

	uploadStmt  *sql.Stmt
	commentStmt *sql.Stmt
	ratingStmt  *sql.Stmt

	overviewMu    sync.RWMutex
	overviewHTML  string
	overviewValid bool

	recipeMu    sync.RWMutex
	recipeCache map[string]string
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

type commentResponse struct {
	Comment string `json:"comment"`
}

type recipeResponse struct {
	ID           string            `json:"id"`
	Title        string            `json:"title"`
	Ingredients  []string          `json:"ingredients"`
	Instructions string            `json:"instructions"`
	Comments     []commentResponse `json:"comments"`
	AvgRating    *float64          `json:"avgRating"`
}

type recipeSummary struct {
	ID        string
	Title     string
	AvgRating sql.NullFloat64
}

type recipeDetail struct {
	ID           string
	Title        string
	Ingredients  []string
	Instructions string
	AvgRating    sql.NullFloat64
	Comments     []string
}

func main() {
	logger := log.New(os.Stdout, "", log.LstdFlags|log.LUTC)

	db, err := openDB()
	if err != nil {
		logger.Fatalf("open database: %v", err)
	}
	defer db.Close()

	ctx, cancel := context.WithTimeout(context.Background(), dbTimeout)
	defer cancel()
	if err := initializeDatabase(ctx, db); err != nil {
		logger.Fatalf("initialize database: %v", err)
	}

	application, err := newApp(logger, db)
	if err != nil {
		logger.Fatalf("prepare statements: %v", err)
	}

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	server := &http.Server{
		Addr:              "0.0.0.0:" + port,
		Handler:           application.routes(),
		ReadHeaderTimeout: 2 * time.Second,
		ReadTimeout:       serverReadTimeout,
		WriteTimeout:      serverWriteTimeout,
		IdleTimeout:       120 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}

	logger.Printf("listening on %s", server.Addr)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		logger.Fatalf("server error: %v", err)
	}
}

func newApp(logger *log.Logger, db *sql.DB) (*app, error) {
	uploadStmt, err := db.Prepare(`
INSERT INTO recipes (id, title, ingredients, instructions)
VALUES ($1, $2, $3, $4)
`)
	if err != nil {
		return nil, err
	}

	commentStmt, err := db.Prepare(`
INSERT INTO comments (id, recipe_id, comment)
SELECT $1, r.id, $3
FROM recipes r
WHERE r.id = $2
`)
	if err != nil {
		return nil, err
	}

	ratingStmt, err := db.Prepare(`
INSERT INTO ratings (id, recipe_id, rating)
SELECT $1, r.id, $3
FROM recipes r
WHERE r.id = $2
`)
	if err != nil {
		return nil, err
	}

	return &app{
		logger:      logger,
		db:          db,
		uploadStmt:  uploadStmt,
		commentStmt: commentStmt,
		ratingStmt:  ratingStmt,
		recipeCache: make(map[string]string),
	}, nil
}

func openDB() (*sql.DB, error) {
	host := os.Getenv("DB_HOST")
	port := os.Getenv("DB_PORT")
	user := os.Getenv("DB_USER")
	password := os.Getenv("DB_PASSWORD")
	name := os.Getenv("DB_NAME")

	if host == "" || user == "" || name == "" {
		return nil, errors.New("database environment variables DB_HOST, DB_USER, and DB_NAME must be set")
	}
	if port == "" {
		port = "5432"
	}

	dsn := fmt.Sprintf(
		"host=%s port=%s user=%s password=%s dbname=%s sslmode=disable connect_timeout=5 application_name=recipe-sharing-app",
		host,
		port,
		user,
		password,
		name,
	)

	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}

	maxOpen := runtime.GOMAXPROCS(0) * 16
	if maxOpen < 32 {
		maxOpen = 32
	}
	db.SetMaxOpenConns(maxOpen)
	db.SetMaxIdleConns(maxOpen / 2)
	db.SetConnMaxIdleTime(5 * time.Minute)
	db.SetConnMaxLifetime(30 * time.Minute)

	ctx, cancel := context.WithTimeout(context.Background(), dbTimeout)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}

	return db, nil
}

func initializeDatabase(ctx context.Context, db *sql.DB) error {
	queries := []string{
		`CREATE TABLE IF NOT EXISTS recipes (
id TEXT PRIMARY KEY,
title TEXT NOT NULL,
ingredients TEXT[] NOT NULL,
instructions TEXT NOT NULL,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)`,
		`CREATE TABLE IF NOT EXISTS comments (
id TEXT PRIMARY KEY,
recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
comment TEXT NOT NULL,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)`,
		`CREATE TABLE IF NOT EXISTS ratings (
id TEXT PRIMARY KEY,
recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
rating SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)`,
		`CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes (created_at DESC)`,
		`CREATE INDEX IF NOT EXISTS idx_comments_recipe_created_at ON comments (recipe_id, created_at)`,
		`CREATE INDEX IF NOT EXISTS idx_ratings_recipe_created_at ON ratings (recipe_id, created_at)`,
		`CREATE INDEX IF NOT EXISTS idx_ratings_recipe_rating ON ratings (recipe_id, rating)`,
	}

	for _, query := range queries {
		if _, err := db.ExecContext(ctx, query); err != nil {
			return err
		}
	}

	return nil
}

func (a *app) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /recipes", a.handleRecipesOverview)
	mux.HandleFunc("POST /recipes/upload", a.handleUploadRecipe)
	mux.HandleFunc("GET /recipes/{recipeId}", a.handleGetRecipe)
	mux.HandleFunc("POST /recipes/{recipeId}/comments", a.handleAddComment)
	mux.HandleFunc("POST /recipes/{recipeId}/ratings", a.handleAddRating)
	return a.recoverPanic(mux)
}

func (a *app) handleRecipesOverview(w http.ResponseWriter, r *http.Request) {
	if htmlPage, ok := a.getOverviewCache(); ok {
		writeHTML(w, http.StatusOK, htmlPage)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), dbTimeout)
	defer cancel()

	recent, err := a.fetchRecentRecipes(ctx, overviewLimit)
	if err != nil {
		a.serverError(w, err)
		return
	}

	topRated, err := a.fetchTopRatedRecipes(ctx, overviewLimit)
	if err != nil {
		a.serverError(w, err)
		return
	}

	htmlPage := renderOverviewHTML(recent, topRated)
	a.setOverviewCache(htmlPage)
	writeHTML(w, http.StatusOK, htmlPage)
}

func (a *app) handleUploadRecipe(w http.ResponseWriter, r *http.Request) {
	var input uploadRecipeRequest
	if err := decodeJSONBody(w, r, &input, uploadBodyLimit); err != nil {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}

	input.Title = strings.TrimSpace(input.Title)
	input.Instructions = strings.TrimSpace(input.Instructions)
	for i := range input.Ingredients {
		input.Ingredients[i] = strings.TrimSpace(input.Ingredients[i])
	}
	if !validRecipeInput(input) {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}

	response := recipeResponse{
		ID:           uuid.NewString(),
		Title:        input.Title,
		Ingredients:  input.Ingredients,
		Instructions: input.Instructions,
		Comments:     make([]commentResponse, 0),
		AvgRating:    nil,
	}

	ctx, cancel := context.WithTimeout(r.Context(), dbTimeout)
	defer cancel()
	if _, err := a.uploadStmt.ExecContext(ctx, response.ID, response.Title, pq.Array(response.Ingredients), response.Instructions); err != nil {
		a.serverError(w, err)
		return
	}

	a.invalidateOverviewCache()
	writeJSON(w, http.StatusCreated, response)
}

func (a *app) handleGetRecipe(w http.ResponseWriter, r *http.Request) {
	recipeID := r.PathValue("recipeId")
	if recipeID == "" {
		http.NotFound(w, r)
		return
	}

	if htmlPage, ok := a.getRecipeCache(recipeID); ok {
		writeHTML(w, http.StatusOK, htmlPage)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), dbTimeout)
	defer cancel()

	detail, err := a.fetchRecipeDetail(ctx, recipeID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			http.NotFound(w, r)
			return
		}
		a.serverError(w, err)
		return
	}

	htmlPage := renderRecipeHTML(detail)
	a.setRecipeCache(recipeID, htmlPage)
	writeHTML(w, http.StatusOK, htmlPage)
}

func (a *app) handleAddComment(w http.ResponseWriter, r *http.Request) {
	recipeID := r.PathValue("recipeId")
	if recipeID == "" {
		http.NotFound(w, r)
		return
	}

	var input addCommentRequest
	if err := decodeJSONBody(w, r, &input, smallBodyLimit); err != nil {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}

	input.Comment = strings.TrimSpace(input.Comment)
	if input.Comment == "" {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), dbTimeout)
	defer cancel()

	result, err := a.commentStmt.ExecContext(ctx, uuid.NewString(), recipeID, input.Comment)
	if err != nil {
		a.serverError(w, err)
		return
	}

	rowsAffected, err := result.RowsAffected()
	if err != nil {
		a.serverError(w, err)
		return
	}
	if rowsAffected == 0 {
		http.NotFound(w, r)
		return
	}

	a.invalidateRecipeCache(recipeID)
	w.WriteHeader(http.StatusCreated)
}

func (a *app) handleAddRating(w http.ResponseWriter, r *http.Request) {
	recipeID := r.PathValue("recipeId")
	if recipeID == "" {
		http.NotFound(w, r)
		return
	}

	var input addRatingRequest
	if err := decodeJSONBody(w, r, &input, smallBodyLimit); err != nil {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}
	if input.Rating < 1 || input.Rating > 5 {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), dbTimeout)
	defer cancel()

	result, err := a.ratingStmt.ExecContext(ctx, uuid.NewString(), recipeID, input.Rating)
	if err != nil {
		a.serverError(w, err)
		return
	}

	rowsAffected, err := result.RowsAffected()
	if err != nil {
		a.serverError(w, err)
		return
	}
	if rowsAffected == 0 {
		http.NotFound(w, r)
		return
	}

	a.invalidateOverviewCache()
	a.invalidateRecipeCache(recipeID)
	w.WriteHeader(http.StatusCreated)
}

func (a *app) fetchRecentRecipes(ctx context.Context, limit int) ([]recipeSummary, error) {
	rows, err := a.db.QueryContext(ctx, `
SELECT r.id, r.title, AVG(rt.rating)::float8 AS avg_rating
FROM recipes r
LEFT JOIN ratings rt ON rt.recipe_id = r.id
GROUP BY r.id, r.title, r.created_at
ORDER BY r.created_at DESC
LIMIT $1
`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	return scanRecipeSummaries(rows)
}

func (a *app) fetchTopRatedRecipes(ctx context.Context, limit int) ([]recipeSummary, error) {
	rows, err := a.db.QueryContext(ctx, `
SELECT r.id, r.title, AVG(rt.rating)::float8 AS avg_rating
FROM recipes r
LEFT JOIN ratings rt ON rt.recipe_id = r.id
GROUP BY r.id, r.title, r.created_at
ORDER BY AVG(rt.rating) DESC NULLS LAST, r.created_at DESC
LIMIT $1
`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	return scanRecipeSummaries(rows)
}

func scanRecipeSummaries(rows *sql.Rows) ([]recipeSummary, error) {
	items := make([]recipeSummary, 0, overviewLimit)
	for rows.Next() {
		var item recipeSummary
		if err := rows.Scan(&item.ID, &item.Title, &item.AvgRating); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return items, nil
}

func (a *app) fetchRecipeDetail(ctx context.Context, recipeID string) (recipeDetail, error) {
	var detail recipeDetail
	row := a.db.QueryRowContext(ctx, `
SELECT r.id, r.title, r.ingredients, r.instructions, AVG(rt.rating)::float8 AS avg_rating
FROM recipes r
LEFT JOIN ratings rt ON rt.recipe_id = r.id
WHERE r.id = $1
GROUP BY r.id, r.title, r.ingredients, r.instructions
`, recipeID)
	if err := row.Scan(&detail.ID, &detail.Title, pq.Array(&detail.Ingredients), &detail.Instructions, &detail.AvgRating); err != nil {
		return recipeDetail{}, err
	}

	rows, err := a.db.QueryContext(ctx, `
SELECT comment
FROM comments
WHERE recipe_id = $1
ORDER BY created_at ASC
`, recipeID)
	if err != nil {
		return recipeDetail{}, err
	}
	defer rows.Close()

	detail.Comments = make([]string, 0)
	for rows.Next() {
		var comment string
		if err := rows.Scan(&comment); err != nil {
			return recipeDetail{}, err
		}
		detail.Comments = append(detail.Comments, comment)
	}
	if err := rows.Err(); err != nil {
		return recipeDetail{}, err
	}

	return detail, nil
}

func validRecipeInput(input uploadRecipeRequest) bool {
	if input.Title == "" || input.Instructions == "" || len(input.Ingredients) == 0 {
		return false
	}
	for _, ingredient := range input.Ingredients {
		if ingredient == "" {
			return false
		}
	}
	return true
}

func decodeJSONBody(w http.ResponseWriter, r *http.Request, dst any, maxBytes int64) error {
	r.Body = http.MaxBytesReader(w, r.Body, maxBytes)
	defer r.Body.Close()

	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(dst); err != nil {
		return err
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return errors.New("request body must contain a single JSON object")
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func writeHTML(w http.ResponseWriter, status int, page string) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(status)
	_, _ = io.WriteString(w, page)
}

func renderOverviewHTML(recent []recipeSummary, topRated []recipeSummary) string {
	var b strings.Builder
	b.Grow(4096)
	b.WriteString("<!doctype html><html><head><meta charset=\"utf-8\"><title>Recipes</title></head><body>")
	b.WriteString("<h1>Recipe Overview</h1>")
	b.WriteString("<section><h2>Recent Recipes</h2><ul>")
	for _, recipe := range recent {
		b.WriteString(renderSummaryListItem(recipe))
	}
	b.WriteString("</ul></section>")
	b.WriteString("<section><h2>Top Rated Recipes</h2><ul>")
	for _, recipe := range topRated {
		b.WriteString(renderSummaryListItem(recipe))
	}
	b.WriteString("</ul></section></body></html>")
	return b.String()
}

func renderSummaryListItem(recipe recipeSummary) string {
	var b strings.Builder
	b.Grow(192)
	b.WriteString("<li><a href=\"/recipes/")
	b.WriteString(html.EscapeString(recipe.ID))
	b.WriteString("\">")
	b.WriteString(html.EscapeString(recipe.Title))
	b.WriteString("</a>")
	if recipe.AvgRating.Valid {
		b.WriteString(" - Avg rating: ")
		b.WriteString(formatFloat(recipe.AvgRating.Float64))
	}
	b.WriteString("</li>")
	return b.String()
}

func renderRecipeHTML(recipe recipeDetail) string {
	var b strings.Builder
	b.Grow(4096)
	b.WriteString("<!doctype html><html><head><meta charset=\"utf-8\"><title>")
	b.WriteString(html.EscapeString(recipe.Title))
	b.WriteString("</title></head><body>")
	b.WriteString("<h1>")
	b.WriteString(html.EscapeString(recipe.Title))
	b.WriteString("</h1><p><strong>ID:</strong> ")
	b.WriteString(html.EscapeString(recipe.ID))
	b.WriteString("</p><p><strong>Average Rating:</strong> ")
	if recipe.AvgRating.Valid {
		b.WriteString(formatFloat(recipe.AvgRating.Float64))
	} else {
		b.WriteString("No ratings yet")
	}
	b.WriteString("</p><h2>Ingredients</h2><ul>")
	for _, ingredient := range recipe.Ingredients {
		b.WriteString("<li>")
		b.WriteString(html.EscapeString(ingredient))
		b.WriteString("</li>")
	}
	b.WriteString("</ul><h2>Instructions</h2><p>")
	b.WriteString(html.EscapeString(recipe.Instructions))
	b.WriteString("</p><h2>Comments</h2><ul>")
	for _, comment := range recipe.Comments {
		b.WriteString("<li>")
		b.WriteString(html.EscapeString(comment))
		b.WriteString("</li>")
	}
	b.WriteString("</ul></body></html>")
	return b.String()
}

func formatFloat(value float64) string {
	return fmt.Sprintf("%.1f", value)
}

func (a *app) getOverviewCache() (string, bool) {
	a.overviewMu.RLock()
	defer a.overviewMu.RUnlock()
	return a.overviewHTML, a.overviewValid
}

func (a *app) setOverviewCache(page string) {
	a.overviewMu.Lock()
	a.overviewHTML = page
	a.overviewValid = true
	a.overviewMu.Unlock()
}

func (a *app) invalidateOverviewCache() {
	a.overviewMu.Lock()
	a.overviewHTML = ""
	a.overviewValid = false
	a.overviewMu.Unlock()
}

func (a *app) getRecipeCache(recipeID string) (string, bool) {
	a.recipeMu.RLock()
	page, ok := a.recipeCache[recipeID]
	a.recipeMu.RUnlock()
	return page, ok
}

func (a *app) setRecipeCache(recipeID, page string) {
	a.recipeMu.Lock()
	a.recipeCache[recipeID] = page
	a.recipeMu.Unlock()
}

func (a *app) invalidateRecipeCache(recipeID string) {
	a.recipeMu.Lock()
	delete(a.recipeCache, recipeID)
	a.recipeMu.Unlock()
}

func (a *app) serverError(w http.ResponseWriter, err error) {
	a.logger.Printf("server error: %v", err)
	http.Error(w, http.StatusText(http.StatusInternalServerError), http.StatusInternalServerError)
}

func (a *app) recoverPanic(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if recovered := recover(); recovered != nil {
				w.Header().Set("Connection", "close")
				a.logger.Printf("panic: %v", recovered)
				http.Error(w, http.StatusText(http.StatusInternalServerError), http.StatusInternalServerError)
			}
		}()
		next.ServeHTTP(w, r)
	})
}
