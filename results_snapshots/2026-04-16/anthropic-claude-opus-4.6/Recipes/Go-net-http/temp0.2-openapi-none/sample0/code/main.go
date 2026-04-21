package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"html/template"
	"log"
	"net/http"
	"os"
	"sort"
	"strings"

	_ "github.com/lib/pq"
	"github.com/google/uuid"
)

var db *sql.DB

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
		dbname = "postgres"
	}

	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, dbname)

	var err error
	db, err = sql.Open("postgres", connStr)
	if err != nil {
		log.Fatalf("Failed to connect to database: %v", err)
	}

	err = db.Ping()
	if err != nil {
		log.Fatalf("Failed to ping database: %v", err)
	}

	createTables()
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
	}

	for _, q := range queries {
		_, err := db.Exec(q)
		if err != nil {
			log.Fatalf("Failed to create table: %v", err)
		}
	}
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

type RecipeOverview struct {
	ID        string
	Title     string
	AvgRating *float64
}

func getPort() string {
	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}
	return port
}

func handleRecipes(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	rows, err := db.Query(`
		SELECT r.id, r.title, AVG(rt.rating) as avg_rating
		FROM recipes r
		LEFT JOIN ratings rt ON r.id = rt.recipe_id
		GROUP BY r.id, r.title, r.created_at
		ORDER BY r.created_at DESC
	`)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	var recipes []RecipeOverview
	for rows.Next() {
		var ro RecipeOverview
		var avgRating sql.NullFloat64
		err := rows.Scan(&ro.ID, &ro.Title, &avgRating)
		if err != nil {
			http.Error(w, "Server error", http.StatusInternalServerError)
			return
		}
		if avgRating.Valid {
			ro.AvgRating = &avgRating.Float64
		}
		recipes = append(recipes, ro)
	}

	// Sort: top-rated first, then recent
	sort.SliceStable(recipes, func(i, j int) bool {
		ri := recipes[i].AvgRating
		rj := recipes[j].AvgRating
		if ri == nil && rj == nil {
			return false
		}
		if ri == nil {
			return false
		}
		if rj == nil {
			return true
		}
		return *ri > *rj
	})

	tmpl := `<!DOCTYPE html>
<html>
<head><title>Recipe Overview</title></head>
<body>
<h1>Recipes</h1>
<ul>
{{range .}}
<li>
<a href="/recipes/{{.ID}}">{{.Title}}</a>
{{if .AvgRating}} - Rating: {{printf "%.1f" (deref .AvgRating)}}{{end}}
</li>
{{end}}
</ul>
</body>
</html>`

	funcMap := template.FuncMap{
		"deref": func(f *float64) float64 {
			if f == nil {
				return 0
			}
			return *f
		},
	}

	t, err := template.New("overview").Funcs(funcMap).Parse(tmpl)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "text/html")
	err = t.Execute(w, recipes)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}
}

func handleUpload(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req UploadRequest
	decoder := json.NewDecoder(r.Body)
	err := decoder.Decode(&req)
	if err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	if req.Title == "" || len(req.Ingredients) == 0 || req.Instructions == "" {
		http.Error(w, "Invalid input: title, ingredients, and instructions are required", http.StatusBadRequest)
		return
	}

	id := uuid.New().String()
	ingredientsJSON, err := json.Marshal(req.Ingredients)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	_, err = db.Exec("INSERT INTO recipes (id, title, ingredients, instructions) VALUES ($1, $2, $3, $4)",
		id, req.Title, string(ingredientsJSON), req.Instructions)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

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

func handleRecipeDetail(w http.ResponseWriter, r *http.Request, recipeID string) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var recipe Recipe
	var ingredientsJSON string
	err := db.QueryRow("SELECT id, title, ingredients, instructions FROM recipes WHERE id = $1", recipeID).
		Scan(&recipe.ID, &recipe.Title, &ingredientsJSON, &recipe.Instructions)
	if err == sql.ErrNoRows {
		http.Error(w, "Recipe not found", http.StatusNotFound)
		return
	} else if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	err = json.Unmarshal([]byte(ingredientsJSON), &recipe.Ingredients)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	// Get comments
	commentRows, err := db.Query("SELECT comment FROM comments WHERE recipe_id = $1 ORDER BY created_at", recipeID)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}
	defer commentRows.Close()

	recipe.Comments = []Comment{}
	for commentRows.Next() {
		var c Comment
		err := commentRows.Scan(&c.Comment)
		if err != nil {
			http.Error(w, "Server error", http.StatusInternalServerError)
			return
		}
		recipe.Comments = append(recipe.Comments, c)
	}

	// Get average rating
	var avgRating sql.NullFloat64
	err = db.QueryRow("SELECT AVG(rating) FROM ratings WHERE recipe_id = $1", recipeID).Scan(&avgRating)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}
	if avgRating.Valid {
		recipe.AvgRating = &avgRating.Float64
	}

	tmpl := `<!DOCTYPE html>
<html>
<head><title>{{.Title}}</title></head>
<body>
<h1>{{.Title}}</h1>
<h2>Ingredients</h2>
<ul>
{{range .Ingredients}}<li>{{.}}</li>{{end}}
</ul>
<h2>Instructions</h2>
<p>{{.Instructions}}</p>
<h2>Rating</h2>
{{if .AvgRating}}<p>Average Rating: {{printf "%.1f" (deref .AvgRating)}}</p>{{else}}<p>No ratings yet</p>{{end}}
<h2>Comments</h2>
{{if .Comments}}
<ul>
{{range .Comments}}<li>{{.Comment}}</li>{{end}}
</ul>
{{else}}
<p>No comments yet</p>
{{end}}
</body>
</html>`

	funcMap := template.FuncMap{
		"deref": func(f *float64) float64 {
			if f == nil {
				return 0
			}
			return *f
		},
	}

	t, err := template.New("recipe").Funcs(funcMap).Parse(tmpl)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "text/html")
	err = t.Execute(w, recipe)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}
}

func handleAddComment(w http.ResponseWriter, r *http.Request, recipeID string) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	// Check recipe exists
	var exists bool
	err := db.QueryRow("SELECT EXISTS(SELECT 1 FROM recipes WHERE id = $1)", recipeID).Scan(&exists)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}
	if !exists {
		http.Error(w, "Recipe not found", http.StatusNotFound)
		return
	}

	var req CommentRequest
	err = json.NewDecoder(r.Body).Decode(&req)
	if err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	if req.Comment == "" {
		http.Error(w, "Invalid input: comment is required", http.StatusBadRequest)
		return
	}

	_, err = db.Exec("INSERT INTO comments (recipe_id, comment) VALUES ($1, $2)", recipeID, req.Comment)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func handleAddRating(w http.ResponseWriter, r *http.Request, recipeID string) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	// Check recipe exists
	var exists bool
	err := db.QueryRow("SELECT EXISTS(SELECT 1 FROM recipes WHERE id = $1)", recipeID).Scan(&exists)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}
	if !exists {
		http.Error(w, "Recipe not found", http.StatusNotFound)
		return
	}

	var req RatingRequest
	err = json.NewDecoder(r.Body).Decode(&req)
	if err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	if req.Rating < 1 || req.Rating > 5 {
		http.Error(w, "Invalid input: rating must be between 1 and 5", http.StatusBadRequest)
		return
	}

	_, err = db.Exec("INSERT INTO ratings (recipe_id, rating) VALUES ($1, $2)", recipeID, req.Rating)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func router(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Path

	// Exact match: /recipes
	if path == "/recipes" || path == "/recipes/" {
		handleRecipes(w, r)
		return
	}

	// /recipes/upload
	if path == "/recipes/upload" {
		handleUpload(w, r)
		return
	}

	// /recipes/{recipeId}/comments
	if strings.HasPrefix(path, "/recipes/") && strings.HasSuffix(path, "/comments") {
		trimmed := strings.TrimPrefix(path, "/recipes/")
		recipeID := strings.TrimSuffix(trimmed, "/comments")
		if recipeID != "" && !strings.Contains(recipeID, "/") {
			handleAddComment(w, r, recipeID)
			return
		}
	}

	// /recipes/{recipeId}/ratings
	if strings.HasPrefix(path, "/recipes/") && strings.HasSuffix(path, "/ratings") {
		trimmed := strings.TrimPrefix(path, "/recipes/")
		recipeID := strings.TrimSuffix(trimmed, "/ratings")
		if recipeID != "" && !strings.Contains(recipeID, "/") {
			handleAddRating(w, r, recipeID)
			return
		}
	}

	// /recipes/{recipeId}
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

	port := getPort()

	http.HandleFunc("/", router)

	addr := fmt.Sprintf("0.0.0.0:%s", port)
	log.Printf("Server starting on %s", addr)
	log.Fatal(http.ListenAndServe(addr, nil))
}