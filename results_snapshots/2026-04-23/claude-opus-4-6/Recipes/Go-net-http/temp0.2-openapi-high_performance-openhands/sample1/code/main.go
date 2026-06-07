package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"html"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

var db *sql.DB

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

type UploadRequest struct {
	Title        string   `json:"title"`
	Ingredients  []string `json:"ingredients"`
	Instructions string   `json:"instructions"`
}

type CommentRequest struct {
	Comment string `json:"comment"`
}

type RatingRequest struct {
	Rating int `json:"rating"`
}

// Simple cache for recipe overview
type overviewCache struct {
	mu      sync.RWMutex
	html    string
	validAt time.Time
	ttl     time.Duration
}

var cache = &overviewCache{ttl: 2 * time.Second}

func (c *overviewCache) get() (string, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if time.Now().Before(c.validAt) {
		return c.html, true
	}
	return "", false
}

func (c *overviewCache) set(h string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.html = h
	c.validAt = time.Now().Add(c.ttl)
}

func (c *overviewCache) invalidate() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.validAt = time.Time{}
}

func initDB() {
	host := os.Getenv("DB_HOST")
	port := os.Getenv("DB_PORT")
	user := os.Getenv("DB_USER")
	password := os.Getenv("DB_PASSWORD")
	dbname := os.Getenv("DB_NAME")

	if host == "" {
		host = "localhost"
	}
	if port == "" {
		port = "5432"
	}
	if user == "" {
		user = "postgres"
	}
	if dbname == "" {
		dbname = "testdb"
	}

	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, dbname)

	var err error
	db, err = sql.Open("postgres", connStr)
	if err != nil {
		panic(err)
	}

	db.SetMaxOpenConns(50)
	db.SetMaxIdleConns(25)
	db.SetConnMaxLifetime(5 * time.Minute)

	for i := 0; i < 30; i++ {
		err = db.Ping()
		if err == nil {
			break
		}
		time.Sleep(time.Second)
	}
	if err != nil {
		panic(err)
	}

	createTables()
}

func createTables() {
	schema := `
	CREATE TABLE IF NOT EXISTS recipes (
		id TEXT PRIMARY KEY,
		title TEXT NOT NULL,
		ingredients TEXT[] NOT NULL,
		instructions TEXT NOT NULL,
		created_at TIMESTAMPTZ DEFAULT NOW()
	);

	CREATE TABLE IF NOT EXISTS comments (
		id SERIAL PRIMARY KEY,
		recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
		comment TEXT NOT NULL,
		created_at TIMESTAMPTZ DEFAULT NOW()
	);

	CREATE INDEX IF NOT EXISTS idx_comments_recipe_id ON comments(recipe_id);

	CREATE TABLE IF NOT EXISTS ratings (
		id SERIAL PRIMARY KEY,
		recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
		rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
		created_at TIMESTAMPTZ DEFAULT NOW()
	);

	CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id ON ratings(recipe_id);
	CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes(created_at DESC);
	`
	if _, err := db.Exec(schema); err != nil {
		panic(err)
	}
}

func handleRecipes(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/recipes")
	path = strings.TrimSuffix(path, "/")

	if path == "" {
		if r.Method == http.MethodGet {
			getRecipesOverview(w, r)
			return
		}
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	if path == "/upload" {
		if r.Method == http.MethodPost {
			uploadRecipe(w, r)
			return
		}
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	// Parse /{recipeId} or /{recipeId}/comments or /{recipeId}/ratings
	parts := strings.SplitN(path[1:], "/", 2) // remove leading /
	recipeID := parts[0]

	if len(parts) == 1 {
		if r.Method == http.MethodGet {
			getRecipe(w, r, recipeID)
			return
		}
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	sub := parts[1]
	if sub == "comments" && r.Method == http.MethodPost {
		addComment(w, r, recipeID)
		return
	}
	if sub == "ratings" && r.Method == http.MethodPost {
		addRating(w, r, recipeID)
		return
	}

	http.NotFound(w, r)
}

func getRecipesOverview(w http.ResponseWriter, r *http.Request) {
	if cached, ok := cache.get(); ok {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Write([]byte(cached))
		return
	}

	rows, err := db.Query(`
		SELECT r.id, r.title, COALESCE(AVG(rt.rating), 0) as avg_rating
		FROM recipes r
		LEFT JOIN ratings rt ON r.id = rt.recipe_id
		GROUP BY r.id, r.title, r.created_at
		ORDER BY r.created_at DESC
		LIMIT 100
	`)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	var sb strings.Builder
	sb.WriteString("<!DOCTYPE html><html><head><title>Recipes</title></head><body>")
	sb.WriteString("<h1>Recipes</h1><ul>")

	for rows.Next() {
		var id, title string
		var avgRating float64
		if err := rows.Scan(&id, &title, &avgRating); err != nil {
			http.Error(w, "Server error", http.StatusInternalServerError)
			return
		}
		sb.WriteString(fmt.Sprintf(`<li><a href="/recipes/%s">%s</a>`, html.EscapeString(id), html.EscapeString(title)))
		if avgRating > 0 {
			sb.WriteString(fmt.Sprintf(` (%.1f/5)`, avgRating))
		}
		sb.WriteString("</li>")
	}

	sb.WriteString("</ul></body></html>")
	result := sb.String()
	cache.set(result)

	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Write([]byte(result))
}

func uploadRecipe(w http.ResponseWriter, r *http.Request) {
	var req UploadRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	if req.Title == "" || len(req.Ingredients) == 0 || req.Instructions == "" {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	id := uuid.New().String()

	// Convert ingredients to PostgreSQL array format
	ingredientsArr := "{" + strings.Join(escapeArrayElements(req.Ingredients), ",") + "}"

	_, err := db.Exec(
		`INSERT INTO recipes (id, title, ingredients, instructions) VALUES ($1, $2, $3::text[], $4)`,
		id, req.Title, ingredientsArr, req.Instructions,
	)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	cache.invalidate()

	recipe := Recipe{
		ID:           id,
		Title:        req.Title,
		Ingredients:  req.Ingredients,
		Instructions: req.Instructions,
		Comments:     []Comment{},
		AvgRating:    nil,
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(recipe)
}

func escapeArrayElements(elems []string) []string {
	result := make([]string, len(elems))
	for i, e := range elems {
		escaped := strings.ReplaceAll(e, `\`, `\\`)
		escaped = strings.ReplaceAll(escaped, `"`, `\"`)
		result[i] = `"` + escaped + `"`
	}
	return result
}

func recipeExists(recipeID string) (bool, error) {
	var exists bool
	err := db.QueryRow("SELECT EXISTS(SELECT 1 FROM recipes WHERE id=$1)", recipeID).Scan(&exists)
	return exists, err
}

func getRecipe(w http.ResponseWriter, r *http.Request, recipeID string) {
	var title, instructions string
	var ingredients []byte
	err := db.QueryRow(
		`SELECT title, ingredients, instructions FROM recipes WHERE id=$1`, recipeID,
	).Scan(&title, &ingredients, &instructions)
	if err == sql.ErrNoRows {
		http.Error(w, "Recipe not found", http.StatusNotFound)
		return
	}
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	ingredientsList := parsePostgresArray(string(ingredients))

	// Get comments
	commentRows, err := db.Query(`SELECT comment FROM comments WHERE recipe_id=$1 ORDER BY created_at`, recipeID)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}
	defer commentRows.Close()

	var comments []string
	for commentRows.Next() {
		var c string
		if err := commentRows.Scan(&c); err != nil {
			http.Error(w, "Server error", http.StatusInternalServerError)
			return
		}
		comments = append(comments, c)
	}

	// Get average rating
	var avgRating sql.NullFloat64
	err = db.QueryRow(`SELECT AVG(rating) FROM ratings WHERE recipe_id=$1`, recipeID).Scan(&avgRating)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	var sb strings.Builder
	sb.WriteString("<!DOCTYPE html><html><head><title>")
	sb.WriteString(html.EscapeString(title))
	sb.WriteString("</title></head><body>")
	sb.WriteString("<h1>")
	sb.WriteString(html.EscapeString(title))
	sb.WriteString("</h1>")

	sb.WriteString("<h2>Ingredients</h2><ul>")
	for _, ing := range ingredientsList {
		sb.WriteString("<li>")
		sb.WriteString(html.EscapeString(ing))
		sb.WriteString("</li>")
	}
	sb.WriteString("</ul>")

	sb.WriteString("<h2>Instructions</h2><p>")
	sb.WriteString(html.EscapeString(instructions))
	sb.WriteString("</p>")

	if avgRating.Valid {
		sb.WriteString(fmt.Sprintf("<h2>Average Rating: %.1f/5</h2>", avgRating.Float64))
	}

	sb.WriteString("<h2>Comments</h2><ul>")
	for _, c := range comments {
		sb.WriteString("<li>")
		sb.WriteString(html.EscapeString(c))
		sb.WriteString("</li>")
	}
	sb.WriteString("</ul>")
	sb.WriteString("</body></html>")

	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Write([]byte(sb.String()))
}

func addComment(w http.ResponseWriter, r *http.Request, recipeID string) {
	var req CommentRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	if req.Comment == "" {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	exists, err := recipeExists(recipeID)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}
	if !exists {
		http.Error(w, "Recipe not found", http.StatusNotFound)
		return
	}

	_, err = db.Exec(`INSERT INTO comments (recipe_id, comment) VALUES ($1, $2)`, recipeID, req.Comment)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func addRating(w http.ResponseWriter, r *http.Request, recipeID string) {
	var req RatingRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	if req.Rating < 1 || req.Rating > 5 {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	exists, err := recipeExists(recipeID)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}
	if !exists {
		http.Error(w, "Recipe not found", http.StatusNotFound)
		return
	}

	_, err = db.Exec(`INSERT INTO ratings (recipe_id, rating) VALUES ($1, $2)`, recipeID, req.Rating)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	cache.invalidate()
	w.WriteHeader(http.StatusCreated)
}

func parsePostgresArray(s string) []string {
	if s == "{}" || s == "" {
		return []string{}
	}
	// Remove surrounding braces
	s = s[1 : len(s)-1]

	var result []string
	var current strings.Builder
	inQuotes := false
	escaped := false

	for i := 0; i < len(s); i++ {
		c := s[i]
		if escaped {
			current.WriteByte(c)
			escaped = false
			continue
		}
		if c == '\\' {
			escaped = true
			continue
		}
		if c == '"' {
			inQuotes = !inQuotes
			continue
		}
		if c == ',' && !inQuotes {
			result = append(result, current.String())
			current.Reset()
			continue
		}
		current.WriteByte(c)
	}
	result = append(result, current.String())
	return result
}

func main() {
	initDB()
	defer db.Close()

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/recipes", handleRecipes)
	mux.HandleFunc("/recipes/", handleRecipes)

	server := &http.Server{
		Addr:         "0.0.0.0:" + port,
		Handler:      mux,
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 10 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	fmt.Printf("Server listening on 0.0.0.0:%s\n", port)
	if err := server.ListenAndServe(); err != nil {
		panic(err)
	}
}
