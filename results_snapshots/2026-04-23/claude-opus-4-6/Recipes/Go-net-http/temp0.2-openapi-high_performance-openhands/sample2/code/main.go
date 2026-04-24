package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"html"
	"log"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

type Comment struct {
	Comment string `json:"comment"`
}

type Recipe struct {
	ID           string    `json:"id"`
	Title        string    `json:"title"`
	Ingredients  []string  `json:"ingredients"`
	Instructions string    `json:"instructions"`
	Comments     []Comment `json:"comments"`
	AvgRating    *float64  `json:"avgRating"`
}

type RecipeUpload struct {
	Title        string   `json:"title"`
	Ingredients  []string `json:"ingredients"`
	Instructions string   `json:"instructions"`
}

type CommentInput struct {
	Comment string `json:"comment"`
}

type RatingInput struct {
	Rating int `json:"rating"`
}

// In-memory cache
type RecipeCache struct {
	mu      sync.RWMutex
	recipes map[string]*Recipe
	order   []string // recipe IDs in creation order
}

var (
	db    *sql.DB
	cache *RecipeCache
)

func newCache() *RecipeCache {
	return &RecipeCache{
		recipes: make(map[string]*Recipe),
	}
}

func (c *RecipeCache) Get(id string) (*Recipe, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	r, ok := c.recipes[id]
	if !ok {
		return nil, false
	}
	// Return a copy
	cp := *r
	cp.Ingredients = make([]string, len(r.Ingredients))
	copy(cp.Ingredients, r.Ingredients)
	cp.Comments = make([]Comment, len(r.Comments))
	copy(cp.Comments, r.Comments)
	if r.AvgRating != nil {
		v := *r.AvgRating
		cp.AvgRating = &v
	}
	return &cp, true
}

func (c *RecipeCache) Set(r *Recipe) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, exists := c.recipes[r.ID]; !exists {
		c.order = append(c.order, r.ID)
	}
	cp := *r
	cp.Ingredients = make([]string, len(r.Ingredients))
	copy(cp.Ingredients, r.Ingredients)
	cp.Comments = make([]Comment, len(r.Comments))
	copy(cp.Comments, r.Comments)
	if r.AvgRating != nil {
		v := *r.AvgRating
		cp.AvgRating = &v
	}
	c.recipes[cp.ID] = &cp
}

func (c *RecipeCache) AddComment(id string, comment string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	r, ok := c.recipes[id]
	if !ok {
		return false
	}
	r.Comments = append(r.Comments, Comment{Comment: comment})
	return true
}

func (c *RecipeCache) AddRating(id string, rating int) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	r, ok := c.recipes[id]
	if !ok {
		return false
	}
	// We need to recalculate from DB or track ratings count
	// For simplicity, we'll invalidate and reload
	_ = rating
	_ = r
	return true
}

func (c *RecipeCache) Invalidate(id string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.recipes, id)
	for i, oid := range c.order {
		if oid == id {
			c.order = append(c.order[:i], c.order[i+1:]...)
			break
		}
	}
}

func (c *RecipeCache) GetAll() []*Recipe {
	c.mu.RLock()
	defer c.mu.RUnlock()
	result := make([]*Recipe, 0, len(c.recipes))
	for _, id := range c.order {
		r := c.recipes[id]
		cp := *r
		cp.Ingredients = make([]string, len(r.Ingredients))
		copy(cp.Ingredients, r.Ingredients)
		cp.Comments = make([]Comment, len(r.Comments))
		copy(cp.Comments, r.Comments)
		if r.AvgRating != nil {
			v := *r.AvgRating
			cp.AvgRating = &v
		}
		result = append(result, &cp)
	}
	return result
}

func initDB() {
	host := getEnv("DB_HOST", "localhost")
	port := getEnv("DB_PORT", "5432")
	user := getEnv("DB_USER", "postgres")
	password := getEnv("DB_PASSWORD", "postgres")
	dbname := getEnv("DB_NAME", "testdb")

	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, dbname)

	var err error
	db, err = sql.Open("postgres", connStr)
	if err != nil {
		log.Fatalf("Failed to open database: %v", err)
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
		log.Fatalf("Failed to connect to database: %v", err)
	}

	createTables()
	loadCache()
}

func createTables() {
	queries := []string{
		`CREATE TABLE IF NOT EXISTS recipes (
			id TEXT PRIMARY KEY,
			title TEXT NOT NULL,
			ingredients TEXT NOT NULL,
			instructions TEXT NOT NULL,
			created_at TIMESTAMP DEFAULT NOW()
		)`,
		`CREATE TABLE IF NOT EXISTS comments (
			id SERIAL PRIMARY KEY,
			recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
			comment TEXT NOT NULL,
			created_at TIMESTAMP DEFAULT NOW()
		)`,
		`CREATE TABLE IF NOT EXISTS ratings (
			id SERIAL PRIMARY KEY,
			recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
			rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
			created_at TIMESTAMP DEFAULT NOW()
		)`,
		`CREATE INDEX IF NOT EXISTS idx_comments_recipe_id ON comments(recipe_id)`,
		`CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id ON ratings(recipe_id)`,
	}

	for _, q := range queries {
		if _, err := db.Exec(q); err != nil {
			log.Fatalf("Failed to execute query: %s, error: %v", q, err)
		}
	}
}

func loadCache() {
	rows, err := db.Query(`SELECT id, title, ingredients, instructions FROM recipes ORDER BY created_at ASC`)
	if err != nil {
		log.Fatalf("Failed to load recipes: %v", err)
	}
	defer rows.Close()

	for rows.Next() {
		var r Recipe
		var ingredients string
		if err := rows.Scan(&r.ID, &r.Title, &ingredients, &r.Instructions); err != nil {
			log.Fatalf("Failed to scan recipe: %v", err)
		}
		r.Ingredients = splitIngredients(ingredients)
		r.Comments = loadComments(r.ID)
		r.AvgRating = loadAvgRating(r.ID)
		cache.Set(&r)
	}
}

func loadComments(recipeID string) []Comment {
	rows, err := db.Query(`SELECT comment FROM comments WHERE recipe_id = $1 ORDER BY created_at ASC`, recipeID)
	if err != nil {
		return nil
	}
	defer rows.Close()

	var comments []Comment
	for rows.Next() {
		var c Comment
		if err := rows.Scan(&c.Comment); err != nil {
			continue
		}
		comments = append(comments, c)
	}
	if comments == nil {
		comments = []Comment{}
	}
	return comments
}

func loadAvgRating(recipeID string) *float64 {
	var avg sql.NullFloat64
	err := db.QueryRow(`SELECT AVG(rating)::float FROM ratings WHERE recipe_id = $1`, recipeID).Scan(&avg)
	if err != nil || !avg.Valid {
		return nil
	}
	v := avg.Float64
	return &v
}

func splitIngredients(s string) []string {
	if s == "" {
		return []string{}
	}
	return strings.Split(s, "|||")
}

func joinIngredients(ingredients []string) string {
	return strings.Join(ingredients, "|||")
}

func getEnv(key, defaultVal string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return defaultVal
}

func handleRecipes(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Path

	if path == "/recipes" || path == "/recipes/" {
		if r.Method == http.MethodGet {
			getRecipesOverview(w, r)
			return
		}
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	if path == "/recipes/upload" || path == "/recipes/upload/" {
		if r.Method == http.MethodPost {
			uploadRecipe(w, r)
			return
		}
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	// Parse /recipes/{id} or /recipes/{id}/comments or /recipes/{id}/ratings
	trimmed := strings.TrimPrefix(path, "/recipes/")
	parts := strings.SplitN(trimmed, "/", 2)
	recipeID := parts[0]

	if len(parts) == 1 {
		if r.Method == http.MethodGet {
			getRecipe(w, r, recipeID)
			return
		}
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	subPath := parts[1]
	switch subPath {
	case "comments":
		if r.Method == http.MethodPost {
			addComment(w, r, recipeID)
			return
		}
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
	case "ratings":
		if r.Method == http.MethodPost {
			addRating(w, r, recipeID)
			return
		}
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
	default:
		http.NotFound(w, r)
	}
}

func getRecipesOverview(w http.ResponseWriter, _ *http.Request) {
	recipes := cache.GetAll()

	// Recent: last 10 by creation order
	recent := recipes
	if len(recent) > 10 {
		recent = recent[len(recent)-10:]
	}

	// Top-rated: sort by avg rating desc, top 10
	rated := make([]*Recipe, 0, len(recipes))
	for _, r := range recipes {
		if r.AvgRating != nil {
			rated = append(rated, r)
		}
	}
	sort.Slice(rated, func(i, j int) bool {
		return *rated[i].AvgRating > *rated[j].AvgRating
	})
	if len(rated) > 10 {
		rated = rated[:10]
	}

	var sb strings.Builder
	sb.WriteString("<!DOCTYPE html><html><head><title>Recipes</title></head><body>")
	sb.WriteString("<h1>Recipes Overview</h1>")

	sb.WriteString("<h2>Recent Recipes</h2><ul>")
	for _, r := range recent {
		sb.WriteString(fmt.Sprintf(`<li><a href="/recipes/%s">%s</a></li>`, html.EscapeString(r.ID), html.EscapeString(r.Title)))
	}
	sb.WriteString("</ul>")

	sb.WriteString("<h2>Top Rated Recipes</h2><ul>")
	for _, r := range rated {
		sb.WriteString(fmt.Sprintf(`<li><a href="/recipes/%s">%s</a> (%.1f)</li>`, html.EscapeString(r.ID), html.EscapeString(r.Title), *r.AvgRating))
	}
	sb.WriteString("</ul>")

	sb.WriteString("</body></html>")

	w.Header().Set("Content-Type", "text/html")
	w.WriteHeader(http.StatusOK)
	w.Write([]byte(sb.String()))
}

func uploadRecipe(w http.ResponseWriter, r *http.Request) {
	var input RecipeUpload
	if err := json.NewDecoder(r.Body).Decode(&input); err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	if input.Title == "" || len(input.Ingredients) == 0 || input.Instructions == "" {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	id := uuid.New().String()
	ingredientsStr := joinIngredients(input.Ingredients)

	_, err := db.Exec(`INSERT INTO recipes (id, title, ingredients, instructions) VALUES ($1, $2, $3, $4)`,
		id, input.Title, ingredientsStr, input.Instructions)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	recipe := &Recipe{
		ID:           id,
		Title:        input.Title,
		Ingredients:  input.Ingredients,
		Instructions: input.Instructions,
		Comments:     []Comment{},
		AvgRating:    nil,
	}
	cache.Set(recipe)

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(recipe)
}

func getRecipe(w http.ResponseWriter, _ *http.Request, recipeID string) {
	recipe, ok := cache.Get(recipeID)
	if !ok {
		// Try loading from DB
		recipe = loadRecipeFromDB(recipeID)
		if recipe == nil {
			http.Error(w, "Recipe not found", http.StatusNotFound)
			return
		}
		cache.Set(recipe)
	}

	var sb strings.Builder
	sb.WriteString("<!DOCTYPE html><html><head><title>")
	sb.WriteString(html.EscapeString(recipe.Title))
	sb.WriteString("</title></head><body>")
	sb.WriteString("<h1>")
	sb.WriteString(html.EscapeString(recipe.Title))
	sb.WriteString("</h1>")

	sb.WriteString("<h2>Ingredients</h2><ul>")
	for _, ing := range recipe.Ingredients {
		sb.WriteString("<li>")
		sb.WriteString(html.EscapeString(ing))
		sb.WriteString("</li>")
	}
	sb.WriteString("</ul>")

	sb.WriteString("<h2>Instructions</h2><p>")
	sb.WriteString(html.EscapeString(recipe.Instructions))
	sb.WriteString("</p>")

	if recipe.AvgRating != nil {
		sb.WriteString(fmt.Sprintf("<h2>Average Rating: %.1f</h2>", *recipe.AvgRating))
	}

	sb.WriteString("<h2>Comments</h2><ul>")
	for _, c := range recipe.Comments {
		sb.WriteString("<li>")
		sb.WriteString(html.EscapeString(c.Comment))
		sb.WriteString("</li>")
	}
	sb.WriteString("</ul>")

	sb.WriteString("</body></html>")

	w.Header().Set("Content-Type", "text/html")
	w.WriteHeader(http.StatusOK)
	w.Write([]byte(sb.String()))
}

func loadRecipeFromDB(recipeID string) *Recipe {
	var r Recipe
	var ingredients string
	err := db.QueryRow(`SELECT id, title, ingredients, instructions FROM recipes WHERE id = $1`, recipeID).
		Scan(&r.ID, &r.Title, &ingredients, &r.Instructions)
	if err != nil {
		return nil
	}
	r.Ingredients = splitIngredients(ingredients)
	r.Comments = loadComments(recipeID)
	r.AvgRating = loadAvgRating(recipeID)
	return &r
}

func addComment(w http.ResponseWriter, r *http.Request, recipeID string) {
	// Check recipe exists
	recipe, ok := cache.Get(recipeID)
	if !ok {
		recipe = loadRecipeFromDB(recipeID)
		if recipe == nil {
			http.Error(w, "Recipe not found", http.StatusNotFound)
			return
		}
		cache.Set(recipe)
	}

	var input CommentInput
	if err := json.NewDecoder(r.Body).Decode(&input); err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	if input.Comment == "" {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	_, err := db.Exec(`INSERT INTO comments (recipe_id, comment) VALUES ($1, $2)`, recipeID, input.Comment)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	cache.AddComment(recipeID, input.Comment)

	w.WriteHeader(http.StatusCreated)
}

func addRating(w http.ResponseWriter, r *http.Request, recipeID string) {
	// Check recipe exists
	_, ok := cache.Get(recipeID)
	if !ok {
		recipe := loadRecipeFromDB(recipeID)
		if recipe == nil {
			http.Error(w, "Recipe not found", http.StatusNotFound)
			return
		}
		cache.Set(recipe)
	}

	var input RatingInput
	if err := json.NewDecoder(r.Body).Decode(&input); err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	if input.Rating < 1 || input.Rating > 5 {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	_, err := db.Exec(`INSERT INTO ratings (recipe_id, rating) VALUES ($1, $2)`, recipeID, input.Rating)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	// Reload avg rating into cache
	avg := loadAvgRating(recipeID)
	cache.mu.Lock()
	if cached, exists := cache.recipes[recipeID]; exists {
		cached.AvgRating = avg
	}
	cache.mu.Unlock()

	w.WriteHeader(http.StatusCreated)
}

func main() {
	cache = newCache()
	initDB()
	defer db.Close()

	port := getEnv("PORT", "5001")

	mux := http.NewServeMux()
	mux.HandleFunc("/recipes", handleRecipes)
	mux.HandleFunc("/recipes/", handleRecipes)

	server := &http.Server{
		Addr:         "0.0.0.0:" + port,
		Handler:      mux,
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 10 * time.Second,
		IdleTimeout:  120 * time.Second,
	}

	log.Printf("Server starting on port %s", port)
	if err := server.ListenAndServe(); err != nil {
		log.Fatalf("Server failed: %v", err)
	}
}
