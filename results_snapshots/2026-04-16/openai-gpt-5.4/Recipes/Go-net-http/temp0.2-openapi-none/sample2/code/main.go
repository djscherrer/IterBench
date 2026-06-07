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

	port := os.Getenv("PORT")
	if strings.TrimSpace(port) == "" {
		port = "5001"
	}

	addr := "0.0.0.0:" + port
	log.Printf("server listening on %s", addr)
	if err := http.ListenAndServe(addr, loggingMiddleware(mux)); err != nil {
		log.Fatalf("server failed: %v", err)
	}
}

func openDBFromEnv() (*sql.DB, error) {
	host := os.Getenv("DB_HOST")
	port := os.Getenv("DB_PORT")
	user := os.Getenv("DB_USER")
	password := os.Getenv("DB_PASSWORD")
	name := os.Getenv("DB_NAME")

	if strings.TrimSpace(host) == "" {
		host = "localhost"
	}
	if strings.TrimSpace(port) == "" {
		port = "5432"
	}
	if strings.TrimSpace(user) == "" || strings.TrimSpace(name) == "" {
		return nil, fmt.Errorf("DB_USER and DB_NAME must be set")
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
			id TEXT PRIMARY KEY,
			title TEXT NOT NULL,
			ingredients TEXT[] NOT NULL,
			instructions TEXT NOT NULL,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)
		`,
		`
		CREATE TABLE IF NOT EXISTS comments (
			id BIGSERIAL PRIMARY KEY,
			recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
			comment TEXT NOT NULL,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)
		`,
		`
		CREATE TABLE IF NOT EXISTS ratings (
			id BIGSERIAL PRIMARY KEY,
			recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
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

	type item struct {
		ID        string
		Title     string
		AvgRating *float64
		CreatedAt time.Time
	}

	var items []item
	for rows.Next() {
		var it item
		var avg sql.NullFloat64
		if err := rows.Scan(&it.ID, &it.Title, &it.CreatedAt, &avg); err != nil {
			http.Error(w, "server error", http.StatusInternalServerError)
			return
		}
		if avg.Valid {
			v := avg.Float64
			it.AvgRating = &v
		}
		items = append(items, it)
	}
	if err := rows.Err(); err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	var b strings.Builder
	b.WriteString("<!doctype html><html><head><title>Recipes</title></head><body>")
	b.WriteString("<h1>Recipe Overview</h1>")
	if len(items) == 0 {
		b.WriteString("<p>No recipes available.</p>")
	} else {
		b.WriteString("<ul>")
		for _, it := range items {
			b.WriteString("<li>")
			b.WriteString(`<a href="/recipes/` + html.EscapeString(it.ID) + `">` + html.EscapeString(it.Title) + `</a>`)
			if it.AvgRating != nil {
				b.WriteString(" - Avg Rating: " + fmt.Sprintf("%.2f", *it.AvgRating))
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
	if err := decodeJSONBody(r, &req); err != nil {
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

	_, err := a.db.Exec(`
		INSERT INTO recipes (id, title, ingredients, instructions)
		VALUES ($1, $2, $3, $4)
	`, id, req.Title, pqStringArray(cleanIngredients), req.Instructions)
	if err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	recipe := Recipe{
		ID:           id,
		Title:        req.Title,
		Ingredients:  cleanIngredients,
		Instructions: req.Instructions,
		Comments:     []Comment{},
		AvgRating:    nil,
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	_ = json.NewEncoder(w).Encode(recipe)
}

func (a *App) handleRecipeRoutes(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/recipes/")
	if path == "" {
		http.NotFound(w, r)
		return
	}

	parts := strings.Split(strings.Trim(path, "/"), "/")
	if len(parts) == 0 || strings.TrimSpace(parts[0]) == "" {
		http.NotFound(w, r)
		return
	}

	recipeID := parts[0]

	if len(parts) == 1 {
		if r.Method != http.MethodGet {
			methodNotAllowed(w, http.MethodGet)
			return
		}
		a.handleGetRecipe(w, r, recipeID)
		return
	}

	if len(parts) == 2 {
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
	var avg sql.NullFloat64

	err := a.db.QueryRow(`
		SELECT
			r.id,
			r.title,
			r.ingredients,
			r.instructions,
			r.created_at,
			AVG(rt.rating)::float8 AS avg_rating
		FROM recipes r
		LEFT JOIN ratings rt ON rt.recipe_id = r.id
		WHERE r.id = $1
		GROUP BY r.id, r.title, r.ingredients, r.instructions, r.created_at
	`, recipeID).Scan(
		&recipe.ID,
		&recipe.Title,
		(*pqStringArray)(&recipe.Ingredients),
		&recipe.Instructions,
		&recipe.CreatedAt,
		&avg,
	)
	if err != nil {
		if err == sql.ErrNoRows {
			http.NotFound(w, r)
			return
		}
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	if avg.Valid {
		v := avg.Float64
		recipe.AvgRating = &v
	}

	commentRows, err := a.db.Query(`
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
	var b strings.Builder
	b.WriteString("<!doctype html><html><head><title>")
	b.WriteString(html.EscapeString(recipe.Title))
	b.WriteString("</title></head><body>")
	b.WriteString("<h1>" + html.EscapeString(recipe.Title) + "</h1>")
	b.WriteString("<h2>Ingredients</h2><ul>")
	for _, ingredient := range recipe.Ingredients {
		b.WriteString("<li>" + html.EscapeString(ingredient) + "</li>")
	}
	b.WriteString("</ul>")
	b.WriteString("<h2>Instructions</h2>")
	b.WriteString("<p>" + html.EscapeString(recipe.Instructions) + "</p>")
	b.WriteString("<h2>Average Rating</h2>")
	if recipe.AvgRating != nil {
		b.WriteString("<p>" + fmt.Sprintf("%.2f", *recipe.AvgRating) + "</p>")
	} else {
		b.WriteString("<p>N/A</p>")
	}
	b.WriteString("<h2>Comments</h2>")
	if len(recipe.Comments) == 0 {
		b.WriteString("<p>No comments yet.</p>")
	} else {
		b.WriteString("<ul>")
		for _, c := range recipe.Comments {
			b.WriteString("<li>" + html.EscapeString(c.Comment) + "</li>")
		}
		b.WriteString("</ul>")
	}
	b.WriteString("</body></html>")
	_, _ = w.Write([]byte(b.String()))
}

func (a *App) handleAddComment(w http.ResponseWriter, r *http.Request, recipeID string) {
	var req addCommentRequest
	if err := decodeJSONBody(r, &req); err != nil {
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
		http.NotFound(w, r)
		return
	}

	_, err = a.db.Exec(`
		INSERT INTO comments (recipe_id, comment)
		VALUES ($1, $2)
	`, recipeID, req.Comment)
	if err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func (a *App) handleAddRating(w http.ResponseWriter, r *http.Request, recipeID string) {
	var req addRatingRequest
	if err := decodeJSONBody(r, &req); err != nil {
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
		http.NotFound(w, r)
		return
	}

	_, err = a.db.Exec(`
		INSERT INTO ratings (recipe_id, rating)
		VALUES ($1, $2)
	`, recipeID, req.Rating)
	if err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func (a *App) recipeExists(recipeID string) (bool, error) {
	var exists bool
	err := a.db.QueryRow(`SELECT EXISTS (SELECT 1 FROM recipes WHERE id = $1)`, recipeID).Scan(&exists)
	return exists, err
}

func decodeJSONBody(r *http.Request, dst any) error {
	if ct := r.Header.Get("Content-Type"); ct != "" && !strings.HasPrefix(strings.ToLower(ct), "application/json") {
		return fmt.Errorf("content type must be application/json")
	}

	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()

	if err := dec.Decode(dst); err != nil {
		return err
	}

	if dec.More() {
		return fmt.Errorf("multiple JSON values in request body")
	}

	return nil
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

type pqStringArray []string

func (a pqStringArray) Value() (driverValue any, err error) {
	if a == nil {
		return "{}", nil
	}
	escaped := make([]string, len(a))
	for i, s := range a {
		s = strings.ReplaceAll(s, `\`, `\\`)
		s = strings.ReplaceAll(s, `"`, `\"`)
		escaped[i] = `"` + s + `"`
	}
	return "{" + strings.Join(escaped, ",") + "}", nil
}

func (a *pqStringArray) Scan(src any) error {
	if src == nil {
		*a = nil
		return nil
	}

	var s string
	switch v := src.(type) {
	case string:
		s = v
	case []byte:
		s = string(v)
	default:
		return fmt.Errorf("unsupported scan type %T", src)
	}

	parsed, err := parsePostgresTextArray(s)
	if err != nil {
		return err
	}
	*a = parsed
	return nil
}

func parsePostgresTextArray(input string) ([]string, error) {
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

	if inQuotes {
		return nil, fmt.Errorf("unterminated quoted string in array")
	}

	result = append(result, current.String())
	return result, nil
}

func init() {
	_ = strconv.IntSize
}