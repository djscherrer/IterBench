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

func main() {
	initDB()
	defer db.Close()

	port := os.Getenv("PORT")
	if port == "" {
		port = "5001"
	}

	http.HandleFunc("/recipes", recipesHandler)
	http.HandleFunc("/recipes/upload", uploadHandler)
	http.HandleFunc("/recipes/", recipeDetailHandler)
	http.HandleFunc("/recipes/{recipeId}/comments", commentHandler)
	http.HandleFunc("/recipes/{recipeId}/ratings", ratingHandler)

	log.Printf("Server starting on 0.0.0.0:%s", port)
	log.Fatal(http.ListenAndServe("0.0.0.0:"+port, nil))
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
	if password == "" {
		password = "postgres"
	}
	if dbname == "" {
		dbname = "recipes"
	}

	connStr := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, password, dbname)

	var err error
	db, err = sql.Open("postgres", connStr)
	if err != nil {
		log.Fatal(err)
	}

	err = db.Ping()
	if err != nil {
		log.Fatal(err)
	}

	createTables()
}

func createTables() {
	recipesTable := `
	CREATE TABLE IF NOT EXISTS recipes (
		id UUID PRIMARY KEY,
		title TEXT NOT NULL,
		ingredients TEXT[] NOT NULL,
		instructions TEXT NOT NULL,
		created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
	);`

	commentsTable := `
	CREATE TABLE IF NOT EXISTS comments (
		id UUID PRIMARY KEY,
		recipe_id UUID REFERENCES recipes(id) ON DELETE CASCADE,
		comment TEXT NOT NULL,
		created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
	);`

	ratingsTable := `
	CREATE TABLE IF NOT EXISTS ratings (
		id UUID PRIMARY KEY,
		recipe_id UUID REFERENCES recipes(id) ON DELETE CASCADE,
		rating INTEGER CHECK (rating >= 1 AND rating <= 5) NOT NULL,
		created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
	);`

	_, err := db.Exec(recipesTable)
	if err != nil {
		log.Fatal(err)
	}
	_, err = db.Exec(commentsTable)
	if err != nil {
		log.Fatal(err)
	}
	_, err = db.Exec(ratingsTable)
	if err != nil {
		log.Fatal(err)
	}
}

func recipesHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	query := `
	SELECT r.id, r.title, COALESCE(AVG(rt.rating), 0) as avg_rating
	FROM recipes r
	LEFT JOIN ratings rt ON r.id = rt.recipe_id
	GROUP BY r.id, r.title
	ORDER BY r.created_at DESC
	LIMIT 20`

	rows, err := db.Query(query)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	var recipes []RecipeSummary
	for rows.Next() {
		var recipe RecipeSummary
		var avgRating float64
		err := rows.Scan(&recipe.ID, &recipe.Title, &avgRating)
		if err != nil {
			http.Error(w, "Server error", http.StatusInternalServerError)
			return
		}
		recipes = append(recipes, recipe)
	}

	tmpl := `
	<!DOCTYPE html>
	<html>
	<head>
		<title>Recipe Overview</title>
	</head>
	<body>
		<h1>Recipe Overview</h1>
		<ul>
			{{range .}}
			<li><a href="/recipes/{{.ID}}">{{.Title}}</a></li>
			{{end}}
		</ul>
	</body>
	</html>`

	t, err := template.New("overview").Parse(tmpl)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "text/html")
	err = t.Execute(w, recipes)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
	}
}

func uploadHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req UploadRequest
	err := json.NewDecoder(r.Body).Decode(&req)
	if err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	if req.Title == "" || len(req.Ingredients) == 0 || req.Instructions == "" {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	id := uuid.New().String()
	_, err = db.Exec("INSERT INTO recipes (id, title, ingredients, instructions) VALUES ($1, $2, $3, $4)",
		id, req.Title, pqArray(req.Ingredients), req.Instructions)
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

func recipeDetailHandler(w http.ResponseWriter, r *http.Request) {
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
	err := db.QueryRow("SELECT id, title, ingredients, instructions FROM recipes WHERE id = $1", recipeID).
		Scan(&recipe.ID, &recipe.Title, &recipe.Ingredients, &recipe.Instructions)
	if err != nil {
		if err == sql.ErrNoRows {
			http.Error(w, "Recipe not found", http.StatusNotFound)
		} else {
			http.Error(w, "Server error", http.StatusInternalServerError)
		}
		return
	}

	rows, err := db.Query("SELECT comment FROM comments WHERE recipe_id = $1 ORDER BY created_at DESC", recipeID)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	var comments []string
	for rows.Next() {
		var comment string
		err := rows.Scan(&comment)
		if err != nil {
			http.Error(w, "Server error", http.StatusInternalServerError)
			return
		}
		comments = append(comments, comment)
	}
	recipe.Comments = comments

	var avgRating *float64
	err = db.QueryRow("SELECT AVG(rating) FROM ratings WHERE recipe_id = $1", recipeID).Scan(&avgRating)
	if err != nil {
		avgRating = nil
	}
	recipe.AvgRating = avgRating

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
		<p>No comments yet.</p>
		{{end}}
	</body>
	</html>`

	t, err := template.New("detail").Parse(tmpl)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "text/html")
	err = t.Execute(w, recipe)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
	}
}

func commentHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	pathParts := strings.Split(r.URL.Path, "/")
	if len(pathParts) < 4 || pathParts[3] != "comments" {
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
	err = json.NewDecoder(r.Body).Decode(&req)
	if err != nil || req.Comment == "" {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	id := uuid.New().String()
	_, err = db.Exec("INSERT INTO comments (id, recipe_id, comment) VALUES ($1, $2, $3)",
		id, recipeID, req.Comment)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func ratingHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	pathParts := strings.Split(r.URL.Path, "/")
	if len(pathParts) < 4 || pathParts[3] != "ratings" {
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
	err = json.NewDecoder(r.Body).Decode(&req)
	if err != nil {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}
	if req.Rating < 1 || req.Rating > 5 {
		http.Error(w, "Invalid input", http.StatusBadRequest)
		return
	}

	id := uuid.New().String()
	_, err = db.Exec("INSERT INTO ratings (id, recipe_id, rating) VALUES ($1, $2, $3)",
		id, recipeID, req.Rating)
	if err != nil {
		http.Error(w, "Server error", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
}

func pqArray(arr []string) string {
	if len(arr) == 0 {
		return "{}"
	}
	escaped := make([]string, len(arr))
	for i, v := range arr {
		escaped[i] = `"` + strings.ReplaceAll(v, `"`, `\"`) + `"`
	}
	return "{" + strings.Join(escaped, ",") + "}"
}