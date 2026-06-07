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

var db *sql.DB

// In-memory cache
type recipeCache struct {
	mu      sync.RWMutex
	recipes map[string]*Recipe
	order   []string // recipe IDs in insertion order
}

var cache = &recipeCache{
	recipes: make(map[string]*Recipe),
}

type Comment struct {
	Comment string `json:"comment"`
}

type Recipe struct {
	mu           sync.RWMutex `json:"-"`
	ID           string       `json:"id"`
	Title        string       `json:"title"`
	Ingredients  []string     `json:"ingredients"`
	Instructions string       `json:"instructions"`
	Comments     []Comment    `json:"comments"`
	AvgRating    *float64     `json:"avgRating"`
	ratings      []int
	createdAt    time.Time
}

func (r *Recipe) clone() *Recipe {
	r.mu.RLock()
	defer r.mu.RUnlock()
	c := &Recipe{
		ID:           r.ID,
		Title:        r.Title,
		Ingredients:  make([]string, len(r.Ingredients)),
		Instructions: r.Instructions,
		Comments:     make([]Comment, len(r.Comments)),
		createdAt:    r.createdAt,
	}
	copy(c.Ingredients, r.Ingredients)
	copy(c.Comments, r.Comments)
	if r.AvgRating != nil {
		avg := *r.AvgRating
		c.AvgRating = &avg
	}
	return c
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
		log.Fatal("Failed to open DB:", err)
	}

	db.SetMaxOpenConns(50)
	db.SetMaxIdleConns(25)
	db.SetConnMaxLifetime(5 * time.Minute)

	if err = db.Ping(); err != nil {
		log.Fatal("Failed to ping DB:", err)
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
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)`,
		`CREATE TABLE IF NOT EXISTS comments (
			id SERIAL PRIMARY KEY,
			recipe_id TEXT NOT NULL REFERENCES recipes(id),
			comment TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS ratings (
			id SERIAL PRIMARY KEY,
			recipe_id TEXT NOT NULL REFERENCES recipes(id),
			rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5)
		)`,
		`CREATE INDEX IF NOT EXISTS idx_comments_recipe_id ON comments(recipe_id)`,
		`CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id ON ratings(recipe_id)`,
		`CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes(created_at)`,
	}
	for _, q := range queries {
		if _, err := db.Exec(q); err != nil {
			log.Fatal("Failed to create table:", err)
		}
	}
}

func loadCache() {
	rows, err := db.Query("SELECT id, title, ingredients, instructions, created_at FROM recipes ORDER BY created_at ASC")
	if err != nil {
		log.Fatal("Failed to load recipes:", err)
	}
	defer rows.Close()

	for rows.Next() {
		var r Recipe
		var ingStr string
		if err := rows.Scan(&r.ID, &r.Title, &ingStr, &r.Instructions, &r.createdAt); err != nil {
			log.Fatal("Failed to scan recipe:", err)
		}
		if err := json.Unmarshal([]byte(ingStr), &r.Ingredients); err != nil {
			r.Ingredients = []string{}
		}
		r.Comments = []Comment{}
		r.ratings = []int{}
		cache.recipes[r.ID] = &r
		cache.order = append(cache.order, r.ID)
	}

	// Load comments
	crows, err := db.Query("SELECT recipe_id, comment FROM comments")
	if err != nil {
		log.Fatal("Failed to load comments:", err)
	}
	defer crows.Close()
	for crows.Next() {
		var rid, comment string
		if err := crows.Scan(&rid, &comment); err != nil {
			log.Fatal("Failed to scan comment:", err)
		}
		if r, ok := cache.recipes[rid]; ok {
			r.Comments = append(r.Comments, Comment{Comment: comment})
		}
	}

	// Load ratings
	rrows, err := db.Query("SELECT recipe_id, rating FROM ratings")
	if err != nil {
		log.Fatal("Failed to load ratings:", err)
	}
	defer rrows.Close()
	for rrows.Next() {
		var rid string
		var rating int
		if err := rrows.Scan(&rid, &rating); err != nil {
			log.Fatal("Failed to scan rating:", err)
		}
		if r, ok := cache.recipes[rid]; ok {
			r.ratings = append(r.ratings, rating)
		}
	}

	// Compute avg ratings
	for _, r := range cache.recipes {
		computeAvg(r)
	}
}

func computeAvg(r *Recipe) {
	if len(r.ratings) == 0 {
		r.AvgRating = nil
		return
	}
	sum := 0
	for _, v := range r.ratings {
		sum += v
	}
	avg := float64(sum) / float64(len(r.ratings))
	r.AvgRating = &avg
}

func handleRecipes(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	cache.mu.RLock()
	// Get recent recipes (last 10)
	n := len(cache.order)
	recentIDs := make([]string, 0, 10)
	for i := n - 1; i >= 0 && len(recentIDs) < 10; i-- {
		recentIDs = append(recentIDs, cache.order[i])
	}

	// Get top rated (top 10 by avg rating)
	type ratedRecipe struct {
		id  string
		avg float64
	}
	rated := make([]ratedRecipe, 0, len(cache.recipes))
	for id, rec := range cache.recipes {
		rec.mu.RLock()
		if rec.AvgRating != nil {
			rated = append(rated, ratedRecipe{id: id, avg: *rec.AvgRating})
		}
		rec.mu.RUnlock()
	}
	cache.mu.RUnlock()

	sort.Slice(rated, func(i, j int) bool {
		return rated[i].avg > rated[j].avg
	})
	if len(rated) > 10 {
		rated = rated[:10]
	}

	var sb strings.Builder
	sb.WriteString("<!DOCTYPE html><html><head><title>Recipes</title></head><body>")
	sb.WriteString("<h1>Recent Recipes</h1><ul>")

	cache.mu.RLock()
	for _, id := range recentIDs {
		if rec, ok := cache.recipes[id]; ok {
			rec.mu.RLock()
			sb.WriteString(fmt.Sprintf(`<li><a href="/recipes/%s">%s</a></li>`, html.EscapeString(id), html.EscapeString(rec.Title)))
			rec.mu.RUnlock()
		}
	}
	sb.WriteString("</ul><h1>Top Rated Recipes</h1><ul>")
	for _, rr := range rated {
		if rec, ok := cache.recipes[rr.id]; ok {
			rec.mu.RLock()
			sb.WriteString(fmt.Sprintf(`<li><a href="/recipes/%s">%s</a> (%.1f)</li>`, html.EscapeString(rr.id), html.EscapeString(rec.Title), rr.avg))
			rec.mu.RUnlock()
		}
	}
	cache.mu.RUnlock()

	sb.WriteString("</ul></body></html>")

	w.Header().Set("Content-Type", "text/html")
	w.WriteHeader(http.StatusOK)
	w.Write([]byte(sb.String()))
}

func handleUpload(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var input struct {
		Title        string   `json:"title"`
		Ingredients  []string `json:"ingredients"`
		Instructions string   `json:"instructions"`
	}
	if err := json.NewDecoder(r.Body).Decode(&input); err != nil {
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}
	if input.Title == "" || len(input.Ingredients) == 0 || input.Instructions == "" {
		http.Error(w, "Missing required fields", http.StatusBadRequest)
		return
	}

	id := uuid.New().String()
	ingJSON, _ := json.Marshal(input.Ingredients)
	now := time.Now()

	_, err := db.Exec("INSERT INTO recipes (id, title, ingredients, instructions, created_at) VALUES ($1, $2, $3, $4, $5)",
		id, input.Title, string(ingJSON), input.Instructions, now)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	rec := &Recipe{
		ID:           id,
		Title:        input.Title,
		Ingredients:  input.Ingredients,
		Instructions: input.Instructions,
		Comments:     []Comment{},
		AvgRating:    nil,
		ratings:      []int{},
		createdAt:    now,
	}

	cache.mu.Lock()
	cache.recipes[id] = rec
	cache.order = append(cache.order, id)
	cache.mu.Unlock()

	resp := rec.clone()
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(resp)
}

func handleRecipeDetail(w http.ResponseWriter, r *http.Request, recipeID string) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	cache.mu.RLock()
	rec, ok := cache.recipes[recipeID]
	cache.mu.RUnlock()

	if !ok {
		http.Error(w, "Recipe not found", http.StatusNotFound)
		return
	}

	rec.mu.RLock()
	title := html.EscapeString(rec.Title)
	instructions := html.EscapeString(rec.Instructions)
	ingredients := make([]string, len(rec.Ingredients))
	copy(ingredients, rec.Ingredients)
	comments := make([]Comment, len(rec.Comments))
	copy(comments, rec.Comments)
	var avgStr string
	if rec.AvgRating != nil {
		avgStr = fmt.Sprintf("%.1f", *rec.AvgRating)
	} else {
		avgStr = "No ratings yet"
	}
	rec.mu.RUnlock()

	var sb strings.Builder
	sb.WriteString("<!DOCTYPE html><html><head><title>")
	sb.WriteString(title)
	sb.WriteString("</title></head><body>")
	sb.WriteString("<h1>")
	sb.WriteString(title)
	sb.WriteString("</h1>")
	sb.WriteString("<h2>Ingredients</h2><ul>")
	for _, ing := range ingredients {
		sb.WriteString("<li>")
		sb.WriteString(html.EscapeString(ing))
		sb.WriteString("</li>")
	}
	sb.WriteString("</ul>")
	sb.WriteString("<h2>Instructions</h2><p>")
	sb.WriteString(instructions)
	sb.WriteString("</p>")
	sb.WriteString("<h2>Average Rating</h2><p>")
	sb.WriteString(avgStr)
	sb.WriteString("</p>")
	sb.WriteString("<h2>Comments</h2><ul>")
	for _, c := range comments {
		sb.WriteString("<li>")
		sb.WriteString(html.EscapeString(c.Comment))
		sb.WriteString("</li>")
	}
	sb.WriteString("</ul></body></html>")

	w.Header().Set("Content-Type", "text/html")
	w.WriteHeader(http.StatusOK)
	w.Write([]byte(sb.String()))
}

func handleAddComment(w http.ResponseWriter, r *http.Request, recipeID string) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	cache.mu.RLock()
	rec, ok := cache.recipes[recipeID]
	cache.mu.RUnlock()

	if !ok {
		http.Error(w, "Recipe not found", http.StatusNotFound)
		return
	}

	var input struct {
		Comment string `json:"comment"`
	}
	if err := json.NewDecoder(r.Body).Decode(&input); err != nil {
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}
	if input.Comment == "" {
		http.Error(w, "Missing comment", http.StatusBadRequest)
		return
	}

	_, err := db.Exec("INSERT INTO comments (recipe_id, comment) VALUES ($1, $2)", recipeID, input.Comment)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	rec.mu.Lock()
	rec.Comments = append(rec.Comments, Comment{Comment: input.Comment})
	rec.mu.Unlock()

	w.WriteHeader(http.StatusCreated)
}

func handleAddRating(w http.ResponseWriter, r *http.Request, recipeID string) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	cache.mu.RLock()
	rec, ok := cache.recipes[recipeID]
	cache.mu.RUnlock()

	if !ok {
		http.Error(w, "Recipe not found", http.StatusNotFound)
		return
	}

	var input struct {
		Rating int `json:"rating"`
	}
	if err := json.NewDecoder(r.Body).Decode(&input); err != nil {
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}
	if input.Rating < 1 || input.Rating > 5 {
		http.Error(w, "Rating must be between 1 and 5", http.StatusBadRequest)
		return
	}

	_, err := db.Exec("INSERT INTO ratings (recipe_id, rating) VALUES ($1, $2)", recipeID, input.Rating)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	rec.mu.Lock()
	rec.ratings = append(rec.ratings, input.Rating)
	computeAvg(rec)
	rec.mu.Unlock()

	w.WriteHeader(http.StatusCreated)
}

func router(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Path

	if path == "/recipes" || path == "/recipes/" {
		if r.Method == http.MethodGet {
			handleRecipes(w, r)
			return
		}
	}

	if path == "/recipes/upload" {
		handleUpload(w, r)
		return
	}

	// Match /recipes/{recipeId}/comments
	if strings.HasSuffix(path, "/comments") {
		trimmed := strings.TrimPrefix(path, "/recipes/")
		recipeID := strings.TrimSuffix(trimmed, "/comments")
		if recipeID != "" && !strings.Contains(recipeID, "/") {
			handleAddComment(w, r, recipeID)
			return
		}
	}

	// Match /recipes/{recipeId}/ratings
	if strings.HasSuffix(path, "/ratings") {
		trimmed := strings.TrimPrefix(path, "/recipes/")
		recipeID := strings.TrimSuffix(trimmed, "/ratings")
		if recipeID != "" && !strings.Contains(recipeID, "/") {
			handleAddRating(w, r, recipeID)
			return
		}
	}

	// Match /recipes/{recipeId}
	if strings.HasPrefix(path, "/recipes/") {
		recipeID := strings.TrimPrefix(path, "/recipes/")
		if recipeID != "" && !strings.Contains(recipeID, "/") {
			handleRecipeDetail(w, r, recipeID)
			return
		}
	}

	http.NotFound(w, r)
}

func main() {
	initDB()
	defer db.Close()

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	server := &http.Server{
		Addr:         "0.0.0.0:" + port,
		Handler:      http.HandlerFunc(router),
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 10 * time.Second,
		IdleTimeout:  120 * time.Second,
	}

	log.Printf("Server starting on port %s", port)
	if err := server.ListenAndServe(); err != nil {
		log.Fatal(err)
	}
}
