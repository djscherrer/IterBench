package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"html/template"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"

	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

var db *sql.DB

type Recipe struct {
	ID           string   `json:"id"`
	Title        string   `json:"title"`
	Ingredients  []string `json:"ingredients"`
	Instructions string   `json:"instructions"`
	Comments     []string `json:"comments"`
	AvgRating    *float64 `json:"avgRating"`
}

type RecipeSummary struct {
	ID    string
	Title string
}

type CommentRequest struct {
	Comment string `json:"comment"`
}

type RatingRequest struct {
	Rating int `json:"rating"`
}

func initDB() {
	var err error
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
	if password == "" {
		password = "postgres"
	}
	if dbname == "" {
		dbname = "recipes"
	}

	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, dbname)

	db, err = sql.Open("postgres", connStr)
	if err != nil {
		log.Fatal(err)
	}

	if err = db.Ping(); err != nil {
		log.Fatal(err)
	}

	createTables := `
	CREATE TABLE IF NOT EXISTS recipes (
		id UUID PRIMARY KEY,
		title TEXT NOT NULL,
		ingredients TEXT[] NOT NULL,
		instructions TEXT NOT NULL,
		created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
	);
	
	CREATE TABLE IF NOT EXISTS comments (
		id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
		recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
		comment TEXT NOT NULL,
		created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
	);
	
	CREATE TABLE IF NOT EXISTS ratings (
		id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
		recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
		rating INTEGER CHECK (rating >= 1 AND rating <= 5) NOT NULL,
		created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
	);
	`

	_, err = db.Exec(createTables)
	if err != nil {
		log.Fatal(err)
	}
}

func getRecipesHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	rows, err := db.Query(`
		SELECT id, title FROM recipes 
		ORDER BY created_at DESC 
		LIMIT 20
	`)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	var recipes []RecipeSummary
	for rows.Next() {
		var r RecipeSummary
		if err := rows.Scan(&r.ID, &r.Title); err != nil {
			http.Error(w, "Server error", http.StatusInternalServerError)
			return
		}
		recipes = append(recipes, r)
	}

	tmpl := `
	<!DOCTYPE html>
	<html>
	<head>
		<title>Recipe Overview</title>
	</head>
	<body>
		<h1>Recipe Overview</h1>
		<h2>Recent Recipes</h2>
		<ul>
			{{range .}}
			<li><a href="/recipes/{{.ID}}">{{.Title}}</a></li>
			{{end}}
		</ul>
	</body>
	</html>
	`

	t, err := template.New("overview").Parse(tmpl)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "text/html")
	if err := t.Execute(w, recipes); err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
	}
}

func uploadRecipeHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		Title        string   `json:"title"`
		Ingredients  []string `json:"ingredients"`
		Instructions string   `json:"instructions"`
	}

	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	if req.Title == "" || len(req.Ingredients) == 0 || req.Instructions == "" {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	id := uuid.New().String()
	_, err := db.Exec(
		"INSERT INTO recipes (id, title, ingredients, instructions) VALUES ($1, $2, $3, $4)",
		id, req.Title, pqArray(req.Ingredients), req.Instructions,
	)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	recipe := Recipe{
		ID:           id,
		Title:        req.Title,
		Ingredients:  req.Ingredients,
		Instructions: req.Instructions,
		Comments:     []string{},
		AvgRating:    nil,
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(recipe)
}

func getRecipeHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	pathParts := strings.Split(r.URL.Path, "/")
	if len(pathParts) < 3 {
		http.Error(w, "Not found", http.StatusNotFound)
		return
	}
	recipeID := pathParts[2]

	var recipe Recipe
	err := db.QueryRow(
		"SELECT id, title, ingredients, instructions FROM recipes WHERE id = $1",
		recipeID,
	).Scan(&recipe.ID, &recipe.Title, pqArrayScanner(&recipe.Ingredients), &recipe.Instructions)
	if err != nil {
		if err == sql.ErrNoRows {
			http.Error(w, "Recipe not found", http.StatusNotFound)
		} else {
			http.Error(w, "Server error", http.StatusInternalServerError)
		}
		return
	}

	rows, err := db.Query("SELECT comment FROM comments WHERE recipe_id = $1 ORDER BY created_at", recipeID)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	for rows.Next() {
		var comment string
		if err := rows.Scan(&comment); err != nil {
			http.Error(w, "Server error", http.StatusInternalServerError)
			return
		}
		recipe.Comments = append(recipe.Comments, comment)
	}

	var avgRating sql.NullFloat64
	err = db.QueryRow(
		"SELECT AVG(rating) FROM ratings WHERE recipe_id = $1",
		recipeID,
	).Scan(&avgRating)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}
	if avgRating.Valid {
		recipe.AvgRating = &avgRating.Float64
	}

	tmpl := `
	<!DOCTYPE html>
	<html>
	<head>
		<title>{{.Title}}</title>
	</head>
	<body>
		<h1>{{.Title}}</h1>
		<h2>Ingredients</h2>
		<ul>
			{{range .Ingredients}}
			<li>{{.}}</li>
			{{end}}
		</ul>
		<h2>Instructions</h2>
		<p>{{.Instructions}}</p>
		<h2>Average Rating: {{if .AvgRating}}{{printf "%.1f" .AvgRating}}{{else}}No ratings yet{{end}}</h2>
		<h2>Comments</h2>
		{{if .Comments}}
		<ul>
			{{range .Comments}}
			<li>{{.}}</li>
			{{end}}
		</ul>
		{{else}}
		<p>No comments yet</p>
		{{end}}
	</body>
	</html>
	`

	t, err := template.New("recipe").Parse(tmpl)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "text/html")
	if err := t.Execute(w, recipe); err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
	}
}

func addCommentHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	pathParts := strings.Split(r.URL.Path, "/")
	if len(pathParts) < 4 {
		http.Error(w, "Not found", http.StatusNotFound)
		return
	}
	recipeID := pathParts[2]

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
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	if req.Comment == "" {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	_, err = db.Exec(
		"INSERT INTO comments (recipe_id, comment) VALUES ($1, $2)",
		recipeID, req.Comment,
	)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func addRatingHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	pathParts := strings.Split(r.URL.Path, "/")
	if len(pathParts) < 4 {
		http.Error(w, "Not found", http.StatusNotFound)
		return
	}
	recipeID := pathParts[2]

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
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	if req.Rating < 1 || req.Rating > 5 {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	_, err = db.Exec(
		"INSERT INTO ratings (recipe_id, rating) VALUES ($1, $2)",
		recipeID, req.Rating,
	)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func pqArray(arr []string) interface{} {
	return arr
}

func pqArrayScanner(target *[]string) interface{} {
	return target
}

func main() {
	initDB()
	defer db.Close()

	http.HandleFunc("/recipes", getRecipesHandler)
	http.HandleFunc("/recipes/upload", uploadRecipeHandler)
	http.HandleFunc("/recipes/", func(w http.ResponseWriter, r *http.Request) {
		pathParts := strings.Split(r.URL.Path, "/")
		if len(pathParts) == 3 {
			getRecipeHandler(w, r)
		} else if len(pathParts) == 4 && pathParts[3] == "comments" {
			addCommentHandler(w, r)
		} else if len(pathParts) == 4 && pathParts[3] == "ratings" {
			addRatingHandler(w, r)
		} else {
			http.Error(w, "Not found", http.StatusNotFound)
		}
	})

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	log.Printf("Server starting on 0.0.0.0:%s", port)
	log.Fatal(http.ListenAndServe("0.0.0.0:"+port, nil))
}