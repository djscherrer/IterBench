package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"html"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

type App struct {
	db *sql.DB
}

type Recipe struct {
	ID           string    `json:"id"`
	Title        string    `json:"title"`
	Ingredients  []string  `json:"ingredients"`
	Instructions string    `json:"instructions"`
	Comments     []Comment `json:"comments"`
	AvgRating    *float64  `json:"avgRating"`
	CreatedAt    time.Time `json:"-"`
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

func main() {
	db, err := openDBFromEnv()
	if err != nil {
		log.Fatalf("failed to connect to database: %v", err)
	}
	defer db.Close()

	if err := initDB(db); err != nil {
		log.Fatalf("failed to initialize database: %v", err)
	}

	app := &App{db: db}

	mux := http.NewServeMux()
	mux.HandleFunc("/recipes", app.handleRecipes)
	mux.HandleFunc("/recipes/upload", app.handleUploadRecipe)
	mux.HandleFunc("/recipes/", app.handleRecipeRoutes)

	addr := "0.0.0.0:" + getEnv("PORT", "5001")
	log.Printf("server listening on %s", addr)
	if err := http.ListenAndServe(addr, loggingMiddleware(mux)); err != nil {
		log.Fatalf("server error: %v", err)
	}
}

func openDBFromEnv() (*sql.DB, error) {
	host := getEnv("DB_HOST", "localhost")
	port := getEnv("DB_PORT", "5432")
	user := os.Getenv("DB_USER")
	password := os.Getenv("DB_PASSWORD")
	name := os.Getenv("DB_NAME")

	connStr := fmt.Sprintf(
		"host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, name,
	)

	db, err := sql.Open("postgres", connStr)
	if err != nil {
		return nil, err
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := db.PingContext(ctx); err != nil {
		return nil, err
	}

	db.SetMaxOpenConns(10)
	db.SetMaxIdleConns(5)
	db.SetConnMaxLifetime(30 * time.Minute)

	return db, nil
}

func initDB(db *sql.DB) error {
	queries := []string{
		`
		CREATE TABLE IF NOT EXISTS recipes (
			id UUID PRIMARY KEY,
			title TEXT NOT NULL,
			ingredients TEXT[] NOT NULL,
			instructions TEXT NOT NULL,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)
		`,
		`
		CREATE TABLE IF NOT EXISTS comments (
			id UUID PRIMARY KEY,
			recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
			comment TEXT NOT NULL,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)
		`,
		`
		CREATE TABLE IF NOT EXISTS ratings (
			id UUID PRIMARY KEY,
			recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
			rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)
		`,
		`CREATE INDEX IF NOT EXISTS idx_comments_recipe_id ON comments(recipe_id)`,
		`CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id ON ratings(recipe_id)`,
		`CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes(created_at DESC)`,
	}

	for _, q := range queries {
		if _, err := db.Exec(q); err != nil {
			return err
		}
	}

	return nil
}

func (a *App) handleRecipes(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/recipes" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodGet {
		methodNotAllowed(w, http.MethodGet)
		return
	}

	rows, err := a.db.Query(`
		SELECT
			r.id,
			r.title,
			COALESCE(AVG(rt.rating)::float8, NULL) AS avg_rating,
			r.created_at
		FROM recipes r
		LEFT JOIN ratings rt ON rt.recipe_id = r.id
		GROUP BY r.id, r.title, r.created_at
		ORDER BY r.created_at DESC, r.title ASC
		LIMIT 50
	`)
	if err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	type item struct {
		ID        string
		Title     string
		AvgRating *float64
		CreatedAt time.Time
	}

	var items []item
	for rows.Next() {
		var it item
		if err := rows.Scan(&it.ID, &it.Title, &it.AvgRating, &it.CreatedAt); err != nil {
			http.Error(w, "server error", http.StatusInternalServerError)
			return
		}
		items = append(items, it)
	}
	if err := rows.Err(); err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusOK)

	var b strings.Builder
	b.WriteString("<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Recipes</title></head><body>")
	b.WriteString("<h1>Recipe Overview</h1>")
	if len(items) == 0 {
		b.WriteString("<p>No recipes available.</p>")
	} else {
		b.WriteString("<ul>")
		for _, it := range items {
			b.WriteString("<li>")
			b.WriteString("<a href=\"/recipes/")
			b.WriteString(html.EscapeString(it.ID))
			b.WriteString("\">")
			b.WriteString(html.EscapeString(it.Title))
			b.WriteString("</a>")
			if it.AvgRating != nil {
				b.WriteString(" - Avg Rating: ")
				b.WriteString(fmt.Sprintf("%.2f", *it.AvgRating))
			} else {
				b.WriteString(" - Avg Rating: N/A")
			}
			b.WriteString("</li>")
		}
		b.WriteString("</ul>")
	}
	b.WriteString("</body></html>")
	_, _ = w.Write([]byte(b.String()))
}

func (a *App) handleUploadRecipe(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/recipes/upload" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}

	var req uploadRecipeRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}

	req.Title = strings.TrimSpace(req.Title)
	req.Instructions = strings.TrimSpace(req.Instructions)

	if req.Title == "" || req.Instructions == "" || len(req.Ingredients) == 0 {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}

	cleanIngredients := make([]string, 0, len(req.Ingredients))
	for _, ingredient := range req.Ingredients {
		ingredient = strings.TrimSpace(ingredient)
		if ingredient == "" {
			http.Error(w, "invalid input", http.StatusBadRequest)
			return
		}
		cleanIngredients = append(cleanIngredients, ingredient)
	}

	id := uuid.New().String()

	_, err := a.db.Exec(`
		INSERT INTO recipes (id, title, ingredients, instructions)
		VALUES ($1, $2, $3, $4)
	`, id, req.Title, pqStringArray(cleanIngredients), req.Instructions)
	if err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	resp := Recipe{
		ID:           id,
		Title:        req.Title,
		Ingredients:  cleanIngredients,
		Instructions: req.Instructions,
		Comments:     []Comment{},
		AvgRating:    nil,
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	_ = json.NewEncoder(w).Encode(resp)
}

func (a *App) handleRecipeRoutes(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/recipes/")
	if path == "" {
		http.NotFound(w, r)
		return
	}

	parts := strings.Split(strings.Trim(path, "/"), "/")
	if len(parts) == 1 {
		if r.Method != http.MethodGet {
			methodNotAllowed(w, http.MethodGet)
			return
		}
		a.handleGetRecipe(w, r, parts[0])
		return
	}

	if len(parts) == 2 {
		recipeID := parts[0]
		switch parts[1] {
		case "comments":
			if r.Method != http.MethodPost {
				methodNotAllowed(w, http.MethodPost)
				return
			}
			a.handleAddComment(w, r, recipeID)
			return
		case "ratings":
			if r.Method != http.MethodPost {
				methodNotAllowed(w, http.MethodPost)
				return
			}
			a.handleAddRating(w, r, recipeID)
			return
		}
	}

	http.NotFound(w, r)
}

func (a *App) handleGetRecipe(w http.ResponseWriter, r *http.Request, recipeID string) {
	var recipe Recipe
	var avgRating sql.NullFloat64

	err := a.db.QueryRow(`
		SELECT
			r.id,
			r.title,
			r.ingredients,
			r.instructions,
			COALESCE(AVG(rt.rating)::float8, NULL) AS avg_rating,
			r.created_at
		FROM recipes r
		LEFT JOIN ratings rt ON rt.recipe_id = r.id
		WHERE r.id = $1
		GROUP BY r.id, r.title, r.ingredients, r.instructions, r.created_at
	`, recipeID).Scan(
		&recipe.ID,
		&recipe.Title,
		pqStringArrayScanner(&recipe.Ingredients),
		&recipe.Instructions,
		&avgRating,
		&recipe.CreatedAt,
	)
	if err == sql.ErrNoRows {
		http.Error(w, "recipe not found", http.StatusNotFound)
		return
	}
	if err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	if avgRating.Valid {
		recipe.AvgRating = &avgRating.Float64
	}

	commentRows, err := a.db.Query(`
		SELECT comment
		FROM comments
		WHERE recipe_id = $1
		ORDER BY created_at ASC
	`, recipeID)
	if err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}
	defer commentRows.Close()

	for commentRows.Next() {
		var c Comment
		if err := commentRows.Scan(&c.Comment); err != nil {
			http.Error(w, "server error", http.StatusInternalServerError)
			return
		}
		recipe.Comments = append(recipe.Comments, c)
	}
	if err := commentRows.Err(); err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusOK)

	var b strings.Builder
	b.WriteString("<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>")
	b.WriteString(html.EscapeString(recipe.Title))
	b.WriteString("</title></head><body>")
	b.WriteString("<h1>")
	b.WriteString(html.EscapeString(recipe.Title))
	b.WriteString("</h1>")

	b.WriteString("<h2>Ingredients</h2><ul>")
	for _, ingredient := range recipe.Ingredients {
		b.WriteString("<li>")
		b.WriteString(html.EscapeString(ingredient))
		b.WriteString("</li>")
	}
	b.WriteString("</ul>")

	b.WriteString("<h2>Instructions</h2><p>")
	b.WriteString(html.EscapeString(recipe.Instructions))
	b.WriteString("</p>")

	b.WriteString("<h2>Average Rating</h2><p>")
	if recipe.AvgRating != nil {
		b.WriteString(html.EscapeString(fmt.Sprintf("%.2f", *recipe.AvgRating)))
	} else {
		b.WriteString("N/A")
	}
	b.WriteString("</p>")

	b.WriteString("<h2>Comments</h2>")
	if len(recipe.Comments) == 0 {
		b.WriteString("<p>No comments yet.</p>")
	} else {
		b.WriteString("<ul>")
		for _, c := range recipe.Comments {
			b.WriteString("<li>")
			b.WriteString(html.EscapeString(c.Comment))
			b.WriteString("</li>")
		}
		b.WriteString("</ul>")
	}

	b.WriteString("</body></html>")
	_, _ = w.Write([]byte(b.String()))
}

func (a *App) handleAddComment(w http.ResponseWriter, r *http.Request, recipeID string) {
	var req addCommentRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}

	req.Comment = strings.TrimSpace(req.Comment)
	if req.Comment == "" {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}

	exists, err := a.recipeExists(recipeID)
	if err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}
	if !exists {
		http.Error(w, "recipe not found", http.StatusNotFound)
		return
	}

	_, err = a.db.Exec(`
		INSERT INTO comments (id, recipe_id, comment)
		VALUES ($1, $2, $3)
	`, uuid.New().String(), recipeID, req.Comment)
	if err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func (a *App) handleAddRating(w http.ResponseWriter, r *http.Request, recipeID string) {
	var req addRatingRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}

	if req.Rating < 1 || req.Rating > 5 {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}

	exists, err := a.recipeExists(recipeID)
	if err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}
	if !exists {
		http.Error(w, "recipe not found", http.StatusNotFound)
		return
	}

	_, err = a.db.Exec(`
		INSERT INTO ratings (id, recipe_id, rating)
		VALUES ($1, $2, $3)
	`, uuid.New().String(), recipeID, req.Rating)
	if err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func (a *App) recipeExists(recipeID string) (bool, error) {
	var exists bool
	err := a.db.QueryRow(`SELECT EXISTS(SELECT 1 FROM recipes WHERE id = $1)`, recipeID).Scan(&exists)
	return exists, err
}

func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		log.Printf("%s %s %d %s", r.Method, r.URL.Path, rec.status, time.Since(start))
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(statusCode int) {
	r.status = statusCode
	r.ResponseWriter.WriteHeader(statusCode)
}

func methodNotAllowed(w http.ResponseWriter, allowed string) {
	w.Header().Set("Allow", allowed)
	http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
}

func getEnv(key, fallback string) string {
	value := os.Getenv(key)
	if value == "" {
		return fallback
	}
	return value
}

type stringArray struct {
	values *[]string
}

func pqStringArray(values []string) stringArray {
	return stringArray{values: &values}
}

func pqStringArrayScanner(target *[]string) stringArray {
	return stringArray{values: target}
}

func (a stringArray) Value() (driverValue interface{}, err error) {
	if a.values == nil {
		return "{}", nil
	}
	var b strings.Builder
	b.WriteString("{")
	for i, v := range *a.values {
		if i > 0 {
			b.WriteString(",")
		}
		b.WriteString(`"`)
		b.WriteString(escapePostgresArrayString(v))
		b.WriteString(`"`)
	}
	b.WriteString("}")
	return b.String(), nil
}

func (a stringArray) Scan(src interface{}) error {
	if src == nil {
		*a.values = nil
		return nil
	}

	var s string
	switch v := src.(type) {
	case string:
		s = v
	case []byte:
		s = string(v)
	default:
		return fmt.Errorf("unsupported array source type %T", src)
	}

	parsed, err := parsePostgresTextArray(s)
	if err != nil {
		return err
	}
	*a.values = parsed
	return nil
}

func escapePostgresArrayString(s string) string {
	s = strings.ReplaceAll(s, `\`, `\\`)
	s = strings.ReplaceAll(s, `"`, `\"`)
	return s
}

func parsePostgresTextArray(s string) ([]string, error) {
	if len(s) < 2 || s[0] != '{' || s[len(s)-1] != '}' {
		return nil, fmt.Errorf("invalid postgres array: %s", s)
	}
	content := s[1 : len(s)-1]
	if content == "" {
		return []string{}, nil
	}

	var result []string
	var current strings.Builder
	inQuotes := false
	escaped := false

	for i := 0; i < len(content); i++ {
		ch := content[i]

		if escaped {
			current.WriteByte(ch)
			escaped = false
			continue
		}

		if ch == '\\' {
			escaped = true
			continue
		}

		if ch == '"' {
			inQuotes = !inQuotes
			continue
		}

		if ch == ',' && !inQuotes {
			result = append(result, current.String())
			current.Reset()
			continue
		}

		current.WriteByte(ch)
	}

	if inQuotes {
		return nil, fmt.Errorf("unterminated quote in postgres array")
	}

	result = append(result, current.String())
	return result, nil
}

func init() {
	_ = strconv.IntSize
}