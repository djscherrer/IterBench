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

	_ "github.com/lib/pq"
	"github.com/google/uuid"
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

type recipeOverviewItem struct {
	ID        string
	Title     string
	AvgRating *float64
	CreatedAt time.Time
}

func main() {
	db, err := openDBFromEnv()
	if err != nil {
		log.Fatalf("database connection error: %v", err)
	}
	defer db.Close()

	if err := initDB(db); err != nil {
		log.Fatalf("database initialization error: %v", err)
	}

	app := &App{db: db}

	mux := http.NewServeMux()
	mux.HandleFunc("/recipes", app.handleRecipes)
	mux.HandleFunc("/recipes/upload", app.handleUploadRecipe)
	mux.HandleFunc("/recipes/", app.handleRecipeRoutes)

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	addr := "0.0.0.0:" + port
	log.Printf("server listening on %s", addr)
	if err := http.ListenAndServe(addr, loggingMiddleware(mux)); err != nil {
		log.Fatalf("server error: %v", err)
	}
}

func openDBFromEnv() (*sql.DB, error) {
	host := os.Getenv("DB_HOST")
	port := os.Getenv("DB_PORT")
	user := os.Getenv("DB_USER")
	password := os.Getenv("DB_PASSWORD")
	name := os.Getenv("DB_NAME")

	if host == "" || port == "" || user == "" || name == "" {
		return nil, fmt.Errorf("DB_HOST, DB_PORT, DB_USER, and DB_NAME must be set")
	}

	dsn := fmt.Sprintf(
		"host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, name,
	)

	db, err := sql.Open("postgres", dsn)
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
	statements := []string{
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
			id BIGSERIAL PRIMARY KEY,
			recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
			comment TEXT NOT NULL,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)
		`,
		`
		CREATE TABLE IF NOT EXISTS ratings (
			id BIGSERIAL PRIMARY KEY,
			recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
			rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)
		`,
		`CREATE INDEX IF NOT EXISTS idx_comments_recipe_id ON comments(recipe_id)`,
		`CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id ON ratings(recipe_id)`,
		`CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes(created_at DESC)`,
	}

	for _, stmt := range statements {
		if _, err := db.Exec(stmt); err != nil {
			return err
		}
	}

	return nil
}

func (app *App) handleRecipes(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/recipes" {
		http.NotFound(w, r)
		return
	}

	if r.Method != http.MethodGet {
		methodNotAllowed(w, http.MethodGet)
		return
	}

	rows, err := app.db.Query(`
		SELECT
			r.id,
			r.title,
			r.created_at,
			AVG(rt.rating)::float8 AS avg_rating
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

	var items []recipeOverviewItem
	for rows.Next() {
		var item recipeOverviewItem
		var avg sql.NullFloat64

		if err := rows.Scan(&item.ID, &item.Title, &item.CreatedAt, &avg); err != nil {
			http.Error(w, "server error", http.StatusInternalServerError)
			return
		}
		if avg.Valid {
			item.AvgRating = &avg.Float64
		}
		items = append(items, item)
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
		b.WriteString("<p>No recipes found.</p>")
	} else {
		b.WriteString("<ul>")
		for _, item := range items {
			b.WriteString("<li>")
			b.WriteString("<a href=\"/recipes/")
			b.WriteString(html.EscapeString(item.ID))
			b.WriteString("\">")
			b.WriteString(html.EscapeString(item.Title))
			b.WriteString("</a>")
			if item.AvgRating != nil {
				b.WriteString(" - Avg Rating: ")
				b.WriteString(fmt.Sprintf("%.2f", *item.AvgRating))
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

func (app *App) handleUploadRecipe(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/recipes/upload" {
		http.NotFound(w, r)
		return
	}

	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}

	var req uploadRecipeRequest
	if err := decodeJSON(r, &req); err != nil {
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

	id := uuid.NewString()

	_, err := app.db.Exec(`
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

	writeJSON(w, http.StatusCreated, resp)
}

func (app *App) handleRecipeRoutes(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/recipes/")
	path = strings.Trim(path, "/")
	if path == "" {
		http.NotFound(w, r)
		return
	}

	parts := strings.Split(path, "/")

	if len(parts) == 1 {
		if r.Method != http.MethodGet {
			methodNotAllowed(w, http.MethodGet)
			return
		}
		app.handleGetRecipe(w, r, parts[0])
		return
	}

	if len(parts) == 2 {
		switch parts[1] {
		case "comments":
			if r.Method != http.MethodPost {
				methodNotAllowed(w, http.MethodPost)
				return
			}
			app.handleAddComment(w, r, parts[0])
			return
		case "ratings":
			if r.Method != http.MethodPost {
				methodNotAllowed(w, http.MethodPost)
				return
			}
			app.handleAddRating(w, r, parts[0])
			return
		}
	}

	http.NotFound(w, r)
}

func (app *App) handleGetRecipe(w http.ResponseWriter, r *http.Request, recipeID string) {
	var recipe Recipe
	var avg sql.NullFloat64

	err := app.db.QueryRow(`
		SELECT
			r.id,
			r.title,
			r.ingredients,
			r.instructions,
			AVG(rt.rating)::float8 AS avg_rating
		FROM recipes r
		LEFT JOIN ratings rt ON rt.recipe_id = r.id
		WHERE r.id = $1
		GROUP BY r.id, r.title, r.ingredients, r.instructions
	`, recipeID).Scan(&recipe.ID, &recipe.Title, pqStringArrayScan(&recipe.Ingredients), &recipe.Instructions, &avg)
	if err == sql.ErrNoRows {
		http.Error(w, "recipe not found", http.StatusNotFound)
		return
	}
	if err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	if avg.Valid {
		recipe.AvgRating = &avg.Float64
	}

	commentRows, err := app.db.Query(`
		SELECT comment
		FROM comments
		WHERE recipe_id = $1
		ORDER BY created_at ASC, id ASC
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
		b.WriteString(fmt.Sprintf("%.2f", *recipe.AvgRating))
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

func (app *App) handleAddComment(w http.ResponseWriter, r *http.Request, recipeID string) {
	var req addCommentRequest
	if err := decodeJSON(r, &req); err != nil {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}

	req.Comment = strings.TrimSpace(req.Comment)
	if req.Comment == "" {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}

	exists, err := app.recipeExists(recipeID)
	if err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}
	if !exists {
		http.Error(w, "recipe not found", http.StatusNotFound)
		return
	}

	_, err = app.db.Exec(`
		INSERT INTO comments (recipe_id, comment)
		VALUES ($1, $2)
	`, recipeID, req.Comment)
	if err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func (app *App) handleAddRating(w http.ResponseWriter, r *http.Request, recipeID string) {
	var req addRatingRequest
	if err := decodeJSON(r, &req); err != nil {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}

	if req.Rating < 1 || req.Rating > 5 {
		http.Error(w, "invalid input", http.StatusBadRequest)
		return
	}

	exists, err := app.recipeExists(recipeID)
	if err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}
	if !exists {
		http.Error(w, "recipe not found", http.StatusNotFound)
		return
	}

	_, err = app.db.Exec(`
		INSERT INTO ratings (recipe_id, rating)
		VALUES ($1, $2)
	`, recipeID, req.Rating)
	if err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func (app *App) recipeExists(recipeID string) (bool, error) {
	var exists bool
	err := app.db.QueryRow(`SELECT EXISTS(SELECT 1 FROM recipes WHERE id = $1)`, recipeID).Scan(&exists)
	return exists, err
}

func decodeJSON(r *http.Request, dst interface{}) error {
	if r.Body == nil {
		return fmt.Errorf("empty body")
	}
	defer r.Body.Close()

	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()

	if err := dec.Decode(dst); err != nil {
		return err
	}

	if dec.More() {
		return fmt.Errorf("unexpected extra data")
	}

	return nil
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func methodNotAllowed(w http.ResponseWriter, allowed string) {
	w.Header().Set("Allow", allowed)
	http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
}

func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		log.Printf("%s %s %s", r.Method, r.URL.Path, time.Since(start).String())
	})
}

type stringArrayValue []string

func pqStringArray(values []string) stringArrayValue {
	return stringArrayValue(values)
}

func pqStringArrayScan(target *[]string) interface {
	sql.Scanner
} {
	return &stringArrayScanner{target: target}
}

type stringArrayScanner struct {
	target *[]string
}

func (s *stringArrayScanner) Scan(src interface{}) error {
	switch v := src.(type) {
	case string:
		parsed, err := parsePostgresTextArray(v)
		if err != nil {
			return err
		}
		*s.target = parsed
		return nil
	case []byte:
		parsed, err := parsePostgresTextArray(string(v))
		if err != nil {
			return err
		}
		*s.target = parsed
		return nil
	case nil:
		*s.target = nil
		return nil
	default:
		return fmt.Errorf("unsupported array source type %T", src)
	}
}

func (a stringArrayValue) Value() (driverValue interface{}, err error) {
	var b strings.Builder
	b.WriteString("{")
	for i, v := range a {
		if i > 0 {
			b.WriteString(",")
		}
		b.WriteString(`"`)
		b.WriteString(strings.ReplaceAll(strings.ReplaceAll(v, `\`, `\\`), `"`, `\"`))
		b.WriteString(`"`)
	}
	b.WriteString("}")
	return b.String(), nil
}

func parsePostgresTextArray(input string) ([]string, error) {
	input = strings.TrimSpace(input)
	if input == "{}" {
		return []string{}, nil
	}
	if len(input) < 2 || input[0] != '{' || input[len(input)-1] != '}' {
		return nil, fmt.Errorf("invalid postgres array format")
	}

	content := input[1 : len(input)-1]
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

	if escaped || inQuotes {
		return nil, fmt.Errorf("invalid postgres array format")
	}

	result = append(result, current.String())
	return result, nil
}

func atoiDefault(s string, fallback int) int {
	n, err := strconv.Atoi(s)
	if err != nil {
		return fallback
	}
	return n
}