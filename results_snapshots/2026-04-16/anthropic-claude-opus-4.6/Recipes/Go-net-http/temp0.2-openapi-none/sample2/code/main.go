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

	if err = db.Ping(); err != nil {
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
		if _, err := db.Exec(q); err != nil {
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

func main() {
	initDB()

	mux := http.NewServeMux()
	mux.HandleFunc("/recipes", handleRecipes)
	mux.HandleFunc("/recipes/", handleRecipesSubpath)
	mux.HandleFunc("/recipes/upload", handleUpload)

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	addr := "0.0.0.0:" + port
	log.Printf("Server listening on %s", addr)
	log.Fatal(http.ListenAndServe(addr, mux))
}

func handleRecipes(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/recipes" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	getRecipesOverview(w, r)
}

func handleUpload(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	uploadRecipe(w, r)
}

func handleRecipesSubpath(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Path

	// Handle /recipes/upload
	if path == "/recipes/upload" {
		handleUpload(w, r)
		return
	}

	// Strip /recipes/ prefix
	trimmed := strings.TrimPrefix(path, "/recipes/")
	if trimmed == "" {
		http.NotFound(w, r)
		return
	}

	parts := strings.SplitN(trimmed, "/", 2)
	recipeID := parts[0]

	if len(parts) == 1 {
		// /recipes/{recipeId}
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		getRecipe(w, r, recipeID)
		return
	}

	subpath := parts[1]
	switch subpath {
	case "comments":
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		addComment(w, r, recipeID)
	case "ratings":
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		addRating(w, r, recipeID)
	default:
		http.NotFound(w, r)
	}
}

func getRecipesOverview(w http.ResponseWriter, _ *http.Request) {
	rows, err := db.Query(`
		SELECT r.id, r.title, AVG(rt.rating)::DOUBLE PRECISION as avg_rating
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
		if err := rows.Scan(&ro.ID, &ro.Title, &avgRating); err != nil {
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
			if f != nil {
				return *f
			}
			return 0
		},
	}

	t, err := template.New("overview").Funcs(funcMap).Parse(tmpl)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "text/html")
	w.WriteHeader(http.StatusOK)
	if err := t.Execute(w, recipes); err != nil {
		log.Printf("Template execution error: %v", err)
	}
}

func uploadRecipe(w http.ResponseWriter, r *http.Request) {
	var req UploadRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
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

	_, err = db.Exec(`INSERT INTO recipes (id, title, ingredients, instructions) VALUES ($1, $2, $3, $4)`,
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

func recipeExists(recipeID string) (bool, error) {
	var exists bool
	err := db.QueryRow(`SELECT EXISTS(SELECT 1 FROM recipes WHERE id = $1)`, recipeID).Scan(&exists)
	return exists, err
}

func getRecipe(w http.ResponseWriter, _ *http.Request, recipeID string) {
	var recipe Recipe
	var ingredientsJSON string

	err := db.QueryRow(`SELECT id, title, ingredients, instructions FROM recipes WHERE id = $1`, recipeID).
		Scan(&recipe.ID, &recipe.Title, &ingredientsJSON, &recipe.Instructions)
	if err == sql.ErrNoRows {
		http.Error(w, "Recipe not found", http.StatusNotFound)
		return
	} else if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	if err := json.Unmarshal([]byte(ingredientsJSON), &recipe.Ingredients); err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	// Get comments
	rows, err := db.Query(`SELECT comment FROM comments WHERE recipe_id = $1 ORDER BY created_at`, recipeID)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	recipe.Comments = []Comment{}
	for rows.Next() {
		var c Comment
		if err := rows.Scan(&c.Comment); err != nil {
			http.Error(w, "Server error", http.StatusInternalServerError)
			return
		}
		recipe.Comments = append(recipe.Comments, c)
	}

	// Get average rating
	var avgRating sql.NullFloat64
	err = db.QueryRow(`SELECT AVG(rating)::DOUBLE PRECISION FROM ratings WHERE recipe_id = $1`, recipeID).Scan(&avgRating)
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
{{range .Ingredients}}<li>{{.}}</li>
{{end}}
</ul>
<h2>Instructions</h2>
<p>{{.Instructions}}</p>
<h2>Average Rating</h2>
<p>{{if .AvgRating}}{{printf "%.1f" (deref .AvgRating)}} / 5{{else}}No ratings yet{{end}}</p>
<h2>Comments</h2>
<ul>
{{range .Comments}}<li>{{.Comment}}</li>
{{end}}
</ul>
</body>
</html>`

	funcMap := template.FuncMap{
		"deref": func(f *float64) float64 {
			if f != nil {
				return *f
			}
			return 0
		},
	}

	t, err := template.New("recipe").Funcs(funcMap).Parse(tmpl)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "text/html")
	w.WriteHeader(http.StatusOK)
	if err := t.Execute(w, recipe); err != nil {
		log.Printf("Template execution error: %v", err)
	}
}

func addComment(w http.ResponseWriter, r *http.Request, recipeID string) {
	exists, err := recipeExists(recipeID)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}
	if !exists {
		http.Error(w, "Recipe not found", http.StatusNotFound)
		return
	}

	var req CommentRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	if req.Comment == "" {
		http.Error(w, "Invalid input: comment is required", http.StatusBadRequest)
		return
	}

	_, err = db.Exec(`INSERT INTO comments (recipe_id, comment) VALUES ($1, $2)`, recipeID, req.Comment)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(map[string]string{"message": "Comment added successfully"})
}

func addRating(w http.ResponseWriter, r *http.Request, recipeID string) {
	exists, err := recipeExists(recipeID)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}
	if !exists {
		http.Error(w, "Recipe not found", http.StatusNotFound)
		return
	}

	var req RatingRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	if req.Rating < 1 || req.Rating > 5 {
		http.Error(w, "Invalid input: rating must be between 1 and 5", http.StatusBadRequest)
		return
	}

	_, err = db.Exec(`INSERT INTO ratings (recipe_id, rating) VALUES ($1, $2)`, recipeID, req.Rating)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(map[string]string{"message": "Rating added successfully"})
}