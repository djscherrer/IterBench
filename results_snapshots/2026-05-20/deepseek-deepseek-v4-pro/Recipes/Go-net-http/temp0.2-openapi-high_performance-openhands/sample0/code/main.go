package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"html"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

var (
	db  *sql.DB
	cache = &overviewCache{}
)

type overviewCache struct {
	mu       sync.RWMutex
	html     string
	expires  time.Time
}

func (c *overviewCache) get() (string, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if time.Now().Before(c.expires) && c.html != "" {
		return c.html, true
	}
	return "", false
}

func (c *overviewCache) set(html string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.html = html
	c.expires = time.Now().Add(5 * time.Minute)
}

func (c *overviewCache) invalidate() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.expires = time.Time{}
}

type Recipe struct {
	ID           string   `json:"id"`
	Title        string   `json:"title"`
	Ingredients  []string `json:"ingredients"`
	Instructions string   `json:"instructions"`
}

type RecipeWithMeta struct {
	Recipe
	Comments   []Comment    `json:"comments,omitempty"`
	AvgRating  *float64     `json:"avgRating,omitempty"`
}

type Comment struct {
	Comment string `json:"comment"`
}

func main() {
	var err error
	dbHost := getEnv("DB_HOST", "localhost")
	dbPort := getEnv("DB_PORT", "5432")
	dbUser := getEnv("DB_USER", "postgres")
	dbPass := getEnv("DB_PASSWORD", "postgres")
	dbName := getEnv("DB_NAME", "testdb")
	port := getEnv("PORT", "5001")

	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		dbHost, dbPort, dbUser, dbPass, dbName)

	db, err = sql.Open("postgres", connStr)
	if err != nil {
		log.Fatalf("failed to open database: %v", err)
	}
	defer db.Close()

	db.SetMaxOpenConns(50)
	db.SetMaxIdleConns(25)
	db.SetConnMaxLifetime(5 * time.Minute)

	if err = db.Ping(); err != nil {
		log.Fatalf("failed to ping database: %v", err)
	}

	if err = initDB(); err != nil {
		log.Fatalf("failed to init database: %v", err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /recipes", handleRecipesOverview)
	mux.HandleFunc("POST /recipes/upload", handleUploadRecipe)
	mux.HandleFunc("GET /recipes/{recipeId}", handleGetRecipe)
	mux.HandleFunc("POST /recipes/{recipeId}/comments", handleAddComment)
	mux.HandleFunc("POST /recipes/{recipeId}/ratings", handleAddRating)

	srv := &http.Server{
		Addr:         "0.0.0.0:" + port,
		Handler:      mux,
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 10 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	log.Printf("Starting server on 0.0.0.0:%s", port)
	if err = srv.ListenAndServe(); err != nil {
		log.Fatalf("server failed: %v", err)
	}
}

func getEnv(key, defaultVal string) string {
	if val := os.Getenv(key); val != "" {
		return val
	}
	return defaultVal
}

func initDB() error {
	query := `
	CREATE TABLE IF NOT EXISTS recipes (
		id UUID PRIMARY KEY,
		title TEXT NOT NULL,
		ingredients JSONB NOT NULL,
		instructions TEXT NOT NULL,
		created_at TIMESTAMPTZ DEFAULT NOW()
	);
	CREATE TABLE IF NOT EXISTS comments (
		id UUID PRIMARY KEY,
		recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
		comment TEXT NOT NULL,
		created_at TIMESTAMPTZ DEFAULT NOW()
	);
	CREATE TABLE IF NOT EXISTS ratings (
		id UUID PRIMARY KEY,
		recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
		rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
		created_at TIMESTAMPTZ DEFAULT NOW()
	);
	CREATE INDEX IF NOT EXISTS idx_comments_recipe ON comments(recipe_id);
	CREATE INDEX IF NOT EXISTS idx_ratings_recipe ON ratings(recipe_id);
	`
	_, err := db.Exec(query)
	return err
}

func handleRecipesOverview(w http.ResponseWriter, r *http.Request) {
	if cached, ok := cache.get(); ok {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Write([]byte(cached))
		return
	}

	rows, err := db.Query(`
		SELECT r.id, r.title, r.ingredients, r.instructions,
			COALESCE(AVG(rt.rating), 0) as avg_rating,
			COUNT(rt.id) as rating_count
		FROM recipes r
		LEFT JOIN ratings rt ON r.id = rt.recipe_id
		GROUP BY r.id
		ORDER BY avg_rating DESC, r.created_at DESC
		LIMIT 100
	`)
	if err != nil {
		http.Error(w, "Internal server error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	var b strings.Builder
	b.WriteString(`<!DOCTYPE html><html><head><title>Recipe Overview</title></head><body>`)
	b.WriteString(`<h1>Recipes</h1>`)
	b.WriteString(`<p><a href="/recipes/upload">Upload a new recipe</a></p>`)
	b.WriteString(`<ul>`)

	hasRecipes := false
	for rows.Next() {
		hasRecipes = true
		var id, title, instructions string
		var ingredientsJSON []byte
		var avgRating float64
		var ratingCount int
		if err := rows.Scan(&id, &title, &ingredientsJSON, &instructions, &avgRating, &ratingCount); err != nil {
			continue
		}
		b.WriteString(fmt.Sprintf(`<li><a href="/recipes/%s">%s</a> (%.1f stars, %d ratings)</li>`,
			html.EscapeString(id), html.EscapeString(title), avgRating, ratingCount))
	}
	if !hasRecipes {
		b.WriteString(`<li>No recipes yet. Be the first to <a href="/recipes/upload">upload one</a>!</li>`)
	}

	b.WriteString(`</ul></body></html>`)
	htmlStr := b.String()
	cache.set(htmlStr)

	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Write([]byte(htmlStr))
}

func handleUploadRecipe(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Title        string   `json:"title"`
		Ingredients  []string `json:"ingredients"`
		Instructions string   `json:"instructions"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, `{"error":"Invalid JSON"}`, http.StatusBadRequest)
		return
	}
	if req.Title == "" || len(req.Ingredients) == 0 || req.Instructions == "" {
		http.Error(w, `{"error":"title, ingredients, and instructions are required"}`, http.StatusBadRequest)
		return
	}

	id := uuid.New().String()
	ingredientsJSON, err := json.Marshal(req.Ingredients)
	if err != nil {
		http.Error(w, `{"error":"Invalid ingredients"}`, http.StatusBadRequest)
		return
	}

	_, err = db.Exec(
		`INSERT INTO recipes (id, title, ingredients, instructions) VALUES ($1, $2, $3, $4)`,
		id, req.Title, ingredientsJSON, req.Instructions,
	)
	if err != nil {
		http.Error(w, `{"error":"Failed to create recipe"}`, http.StatusInternalServerError)
		return
	}

	cache.invalidate()

	resp := Recipe{
		ID:           id,
		Title:        req.Title,
		Ingredients:  req.Ingredients,
		Instructions: req.Instructions,
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(resp)
}

func handleGetRecipe(w http.ResponseWriter, r *http.Request) {
	recipeID := r.PathValue("recipeId")
	if recipeID == "" {
		http.Error(w, "Recipe ID required", http.StatusBadRequest)
		return
	}

	var id, title, instructions string
	var ingredientsJSON []byte
	err := db.QueryRow(
		`SELECT id, title, ingredients, instructions FROM recipes WHERE id = $1`,
		recipeID,
	).Scan(&id, &title, &ingredientsJSON, &instructions)
	if err == sql.ErrNoRows {
		http.Error(w, "Recipe not found", http.StatusNotFound)
		return
	}
	if err != nil {
		http.Error(w, "Internal server error", http.StatusInternalServerError)
		return
	}

	var ingredients []string
	json.Unmarshal(ingredientsJSON, &ingredients)

	// Get comments
	commentRows, err := db.Query(`SELECT comment FROM comments WHERE recipe_id = $1 ORDER BY created_at DESC`, recipeID)
	if err != nil {
		http.Error(w, "Internal server error", http.StatusInternalServerError)
		return
	}
	defer commentRows.Close()

	var comments []string
	for commentRows.Next() {
		var c string
		if err := commentRows.Scan(&c); err != nil {
			continue
		}
		comments = append(comments, c)
	}

	// Get average rating
	var avgRating sql.NullFloat64
	db.QueryRow(`SELECT AVG(rating) FROM ratings WHERE recipe_id = $1`, recipeID).Scan(&avgRating)

	var b strings.Builder
	b.WriteString(`<!DOCTYPE html><html><head><title>`)
	b.WriteString(html.EscapeString(title))
	b.WriteString(`</title></head><body>`)
	b.WriteString(`<h1>`)
	b.WriteString(html.EscapeString(title))
	b.WriteString(`</h1>`)

	b.WriteString(`<h2>Ingredients</h2><ul>`)
	for _, ing := range ingredients {
		b.WriteString(`<li>`)
		b.WriteString(html.EscapeString(ing))
		b.WriteString(`</li>`)
	}
	b.WriteString(`</ul>`)

	b.WriteString(`<h2>Instructions</h2><p>`)
	b.WriteString(html.EscapeString(instructions))
	b.WriteString(`</p>`)

	if avgRating.Valid {
		b.WriteString(fmt.Sprintf(`<h2>Rating</h2><p>Average: %.1f / 5</p>`, avgRating.Float64))
	} else {
		b.WriteString(`<h2>Rating</h2><p>No ratings yet</p>`)
	}

	b.WriteString(`<h3>Rate this recipe:</h3>`)
	b.WriteString(`<form action="/recipes/` + html.EscapeString(recipeID) + `/ratings" method="POST">`)
	b.WriteString(`<select name="rating">`)
	for i := 1; i <= 5; i++ {
		b.WriteString(fmt.Sprintf(`<option value="%d">%d</option>`, i, i))
	}
	b.WriteString(`</select>`)
	b.WriteString(`<button type="submit">Rate</button></form>`)

	b.WriteString(`<h2>Comments</h2>`)
	if len(comments) > 0 {
		b.WriteString(`<ul>`)
		for _, c := range comments {
			b.WriteString(`<li>`)
			b.WriteString(html.EscapeString(c))
			b.WriteString(`</li>`)
		}
		b.WriteString(`</ul>`)
	} else {
		b.WriteString(`<p>No comments yet.</p>`)
	}

	b.WriteString(`<h3>Add a comment:</h3>`)
	b.WriteString(`<form action="/recipes/` + html.EscapeString(recipeID) + `/comments" method="POST">`)
	b.WriteString(`<textarea name="comment" rows="3" cols="50" required></textarea><br>`)
	b.WriteString(`<button type="submit">Submit Comment</button></form>`)

	b.WriteString(`<p><a href="/recipes">Back to overview</a></p>`)
	b.WriteString(`</body></html>`)

	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Write([]byte(b.String()))
}

func handleAddComment(w http.ResponseWriter, r *http.Request) {
	recipeID := r.PathValue("recipeId")
	if recipeID == "" {
		http.Error(w, "Recipe ID required", http.StatusBadRequest)
		return
	}

	// Try to parse JSON first, fallback to form data
	var comment string
	contentType := r.Header.Get("Content-Type")
	if strings.Contains(contentType, "application/json") {
		var req struct {
			Comment string `json:"comment"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, `{"error":"Invalid JSON"}`, http.StatusBadRequest)
			return
		}
		comment = req.Comment
	} else {
		// Form data
		if err := r.ParseForm(); err != nil {
			http.Error(w, "Invalid form data", http.StatusBadRequest)
			return
		}
		comment = r.FormValue("comment")
	}

	if comment == "" {
		http.Error(w, "Comment is required", http.StatusBadRequest)
		return
	}

	// Check recipe exists
	var exists bool
	err := db.QueryRow(`SELECT EXISTS(SELECT 1 FROM recipes WHERE id = $1)`, recipeID).Scan(&exists)
	if err != nil || !exists {
		http.Error(w, "Recipe not found", http.StatusNotFound)
		return
	}

	id := uuid.New().String()
	_, err = db.Exec(`INSERT INTO comments (id, recipe_id, comment) VALUES ($1, $2, $3)`, id, recipeID, comment)
	if err != nil {
		http.Error(w, "Failed to add comment", http.StatusInternalServerError)
		return
	}

	if strings.Contains(contentType, "application/json") {
		w.WriteHeader(http.StatusCreated)
	} else {
		http.Redirect(w, r, "/recipes/"+recipeID, http.StatusSeeOther)
	}
}

func handleAddRating(w http.ResponseWriter, r *http.Request) {
	recipeID := r.PathValue("recipeId")
	if recipeID == "" {
		http.Error(w, "Recipe ID required", http.StatusBadRequest)
		return
	}

	var rating int
	contentType := r.Header.Get("Content-Type")
	if strings.Contains(contentType, "application/json") {
		var req struct {
			Rating int `json:"rating"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, `{"error":"Invalid JSON"}`, http.StatusBadRequest)
			return
		}
		rating = req.Rating
	} else {
		if err := r.ParseForm(); err != nil {
			http.Error(w, "Invalid form data", http.StatusBadRequest)
			return
		}
		fmt.Sscanf(r.FormValue("rating"), "%d", &rating)
	}

	if rating < 1 || rating > 5 {
		http.Error(w, "Rating must be between 1 and 5", http.StatusBadRequest)
		return
	}

	var exists bool
	err := db.QueryRow(`SELECT EXISTS(SELECT 1 FROM recipes WHERE id = $1)`, recipeID).Scan(&exists)
	if err != nil || !exists {
		http.Error(w, "Recipe not found", http.StatusNotFound)
		return
	}

	id := uuid.New().String()
	_, err = db.Exec(`INSERT INTO ratings (id, recipe_id, rating) VALUES ($1, $2, $3)`, id, recipeID, rating)
	if err != nil {
		http.Error(w, "Failed to add rating", http.StatusInternalServerError)
		return
	}

	if strings.Contains(contentType, "application/json") {
		w.WriteHeader(http.StatusCreated)
	} else {
		http.Redirect(w, r, "/recipes/"+recipeID, http.StatusSeeOther)
	}
}
