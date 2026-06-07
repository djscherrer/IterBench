package main

import (
	"database/sql"
	"database/sql/driver"
	"encoding/json"
	"fmt"
	"html"
	"log"
	"net/http"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

var (
	db         *sql.DB
	recipeIDRe = regexp.MustCompile(`^/recipes/([^/]+)$`)
	commentRe  = regexp.MustCompile(`^/recipes/([^/]+)/comments$`)
	ratingRe   = regexp.MustCompile(`^/recipes/([^/]+)/ratings$`)
	pageCache  recipePageCache
)

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

type overviewCache struct {
	sync.RWMutex
	html      string
	expiresAt time.Time
}

type cachedRecipePage struct {
	html    string
	version uint64
}

type recipePageCache struct {
	sync.RWMutex
	pages    map[string]cachedRecipePage
	versions map[string]uint64
	known    map[string]struct{}
}

var ovCache overviewCache

func (c *recipePageCache) ensureLocked() {
	if c.pages == nil {
		c.pages = make(map[string]cachedRecipePage)
		c.versions = make(map[string]uint64)
		c.known = make(map[string]struct{})
	}
}

func getEnv(key, def string) string {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	return v
}

func initDB() error {
	host := getEnv("DB_HOST", "localhost")
	port := getEnv("DB_PORT", "5432")
	user := getEnv("DB_USER", "postgres")
	pass := getEnv("DB_PASSWORD", "postgres")
	name := getEnv("DB_NAME", "testdb")

	dsn := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
		host, port, user, pass, name)

	var err error
	for i := 0; i < 30; i++ {
		db, err = sql.Open("postgres", dsn)
		if err == nil {
			if err = db.Ping(); err == nil {
				break
			}
		}
		time.Sleep(time.Second)
	}
	if err != nil {
		return err
	}

	db.SetMaxOpenConns(50)
	db.SetMaxIdleConns(25)
	db.SetConnMaxLifetime(5 * time.Minute)
	db.SetConnMaxIdleTime(2 * time.Minute)

	schema := `
	CREATE TABLE IF NOT EXISTS recipes (
		id TEXT PRIMARY KEY,
		title TEXT NOT NULL,
		ingredients TEXT[] NOT NULL,
		instructions TEXT NOT NULL,
		created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
	);
	CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes(created_at DESC);

	CREATE TABLE IF NOT EXISTS comments (
		id BIGSERIAL PRIMARY KEY,
		recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
		comment TEXT NOT NULL,
		created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
	);
	CREATE INDEX IF NOT EXISTS idx_comments_recipe_id ON comments(recipe_id, created_at);

	CREATE TABLE IF NOT EXISTS ratings (
		id BIGSERIAL PRIMARY KEY,
		recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
		rating SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
		created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
	);
	CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id ON ratings(recipe_id);

	CREATE TABLE IF NOT EXISTS recipe_stats (
		recipe_id TEXT PRIMARY KEY REFERENCES recipes(id) ON DELETE CASCADE,
		rating_sum BIGINT NOT NULL DEFAULT 0,
		rating_count BIGINT NOT NULL DEFAULT 0
	);
	`
	_, err = db.Exec(schema)
	return err
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func recipesHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method Not Allowed", http.StatusMethodNotAllowed)
		return
	}
	handleOverview(w, r)
}

func handleOverview(w http.ResponseWriter, r *http.Request) {
	now := time.Now()
	ovCache.RLock()
	if ovCache.html != "" && now.Before(ovCache.expiresAt) {
		cached := ovCache.html
		ovCache.RUnlock()
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Write([]byte(cached))
		return
	}
	ovCache.RUnlock()

	const q = `
		SELECT r.id, r.title,
			CASE WHEN s.rating_count IS NULL OR s.rating_count = 0 THEN NULL
			     ELSE s.rating_sum::float / s.rating_count END AS avg_rating
		FROM recipes r
		LEFT JOIN recipe_stats s ON s.recipe_id = r.id
		ORDER BY r.created_at DESC
		LIMIT 50
	`
	rows, err := db.Query(q)
	if err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	var sb strings.Builder
	sb.WriteString("<!DOCTYPE html><html><head><title>Recipes</title></head><body><h1>Recipes</h1><ul>")
	for rows.Next() {
		var id, title string
		var avg sql.NullFloat64
		if err := rows.Scan(&id, &title, &avg); err != nil {
			continue
		}
		ratingStr := ""
		if avg.Valid {
			ratingStr = fmt.Sprintf(" (avg rating: %.2f)", avg.Float64)
		}
		sb.WriteString(fmt.Sprintf(`<li><a href="/recipes/%s">%s</a>%s</li>`,
			html.EscapeString(id), html.EscapeString(title), ratingStr))
	}
	sb.WriteString("</ul></body></html>")

	out := sb.String()
	ovCache.Lock()
	ovCache.html = out
	ovCache.expiresAt = now.Add(2 * time.Second)
	ovCache.Unlock()

	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Write([]byte(out))
}

func uploadHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method Not Allowed", http.StatusMethodNotAllowed)
		return
	}
	var req UploadRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}
	if strings.TrimSpace(req.Title) == "" || len(req.Ingredients) == 0 || strings.TrimSpace(req.Instructions) == "" {
		writeError(w, http.StatusBadRequest, "missing required fields")
		return
	}
	id := uuid.New().String()

	tx, err := db.Begin()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "server error")
		return
	}
	defer tx.Rollback()

	if _, err := tx.Exec(
		`INSERT INTO recipes (id, title, ingredients, instructions) VALUES ($1,$2,$3,$4)`,
		id, req.Title, pqArray(req.Ingredients), req.Instructions,
	); err != nil {
		writeError(w, http.StatusInternalServerError, "server error")
		return
	}
	if _, err := tx.Exec(`INSERT INTO recipe_stats (recipe_id) VALUES ($1)`, id); err != nil {
		writeError(w, http.StatusInternalServerError, "server error")
		return
	}
	if err := tx.Commit(); err != nil {
		writeError(w, http.StatusInternalServerError, "server error")
		return
	}

	ovCache.Lock()
	ovCache.html = ""
	ovCache.Unlock()
	pageCache.markKnown(id)

	resp := Recipe{
		ID:           id,
		Title:        req.Title,
		Ingredients:  req.Ingredients,
		Instructions: req.Instructions,
		Comments:     []Comment{},
		AvgRating:    nil,
	}
	writeJSON(w, http.StatusCreated, resp)
}

func recipeDispatch(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Path

	if r.Method == http.MethodPost {
		if m := commentRe.FindStringSubmatch(path); m != nil {
			handleAddComment(w, r, m[1])
			return
		}
		if m := ratingRe.FindStringSubmatch(path); m != nil {
			handleAddRating(w, r, m[1])
			return
		}
	}
	if r.Method == http.MethodGet {
		if m := recipeIDRe.FindStringSubmatch(path); m != nil {
			handleGetRecipe(w, r, m[1])
			return
		}
	}
	http.NotFound(w, r)
}

func handleGetRecipe(w http.ResponseWriter, r *http.Request, id string) {
	if cached, ok := pageCache.get(id); ok {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Write([]byte(cached))
		return
	}
	version := pageCache.version(id)

	const q = `
		SELECT r.id, r.title, r.ingredients, r.instructions,
			CASE WHEN s.rating_count IS NULL OR s.rating_count = 0 THEN NULL
			     ELSE s.rating_sum::float / s.rating_count END AS avg_rating
		FROM recipes r
		LEFT JOIN recipe_stats s ON s.recipe_id = r.id
		WHERE r.id = $1
	`
	var rid, title, instructions string
	var ingredientsArr stringArray
	var avg sql.NullFloat64
	err := db.QueryRow(q, id).Scan(&rid, &title, &ingredientsArr, &instructions, &avg)
	if err == sql.ErrNoRows {
		http.NotFound(w, r)
		return
	}
	if err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}

	rows, err := db.Query(`SELECT comment FROM comments WHERE recipe_id=$1 ORDER BY created_at`, id)
	if err != nil {
		http.Error(w, "server error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	var sb strings.Builder
	sb.WriteString("<!DOCTYPE html><html><head><title>")
	sb.WriteString(html.EscapeString(title))
	sb.WriteString("</title></head><body><h1>")
	sb.WriteString(html.EscapeString(title))
	sb.WriteString("</h1><h2>Ingredients</h2><ul>")
	for _, ing := range ingredientsArr {
		sb.WriteString("<li>")
		sb.WriteString(html.EscapeString(ing))
		sb.WriteString("</li>")
	}
	sb.WriteString("</ul><h2>Instructions</h2><p>")
	sb.WriteString(html.EscapeString(instructions))
	sb.WriteString("</p><h2>Rating</h2><p>")
	if avg.Valid {
		sb.WriteString(fmt.Sprintf("%.2f", avg.Float64))
	} else {
		sb.WriteString("No ratings yet")
	}
	sb.WriteString("</p><h2>Comments</h2><ul>")
	for rows.Next() {
		var c string
		if err := rows.Scan(&c); err == nil {
			sb.WriteString("<li>")
			sb.WriteString(html.EscapeString(c))
			sb.WriteString("</li>")
		}
	}
	sb.WriteString("</ul></body></html>")

	out := sb.String()
	pageCache.store(id, version, out)
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Write([]byte(out))
}

func handleAddComment(w http.ResponseWriter, r *http.Request, recipeID string) {
	var req CommentRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}
	if strings.TrimSpace(req.Comment) == "" {
		writeError(w, http.StatusBadRequest, "comment required")
		return
	}

	exists, err := recipeExists(recipeID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "server error")
		return
	}
	if !exists {
		writeError(w, http.StatusNotFound, "recipe not found")
		return
	}

	if _, err := db.Exec(`INSERT INTO comments (recipe_id, comment) VALUES ($1,$2)`, recipeID, req.Comment); err != nil {
		writeError(w, http.StatusInternalServerError, "server error")
		return
	}
	pageCache.invalidate(recipeID)
	writeJSON(w, http.StatusCreated, map[string]string{"status": "created"})
}

func handleAddRating(w http.ResponseWriter, r *http.Request, recipeID string) {
	var req RatingRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid input")
		return
	}
	if req.Rating < 1 || req.Rating > 5 {
		writeError(w, http.StatusBadRequest, "rating must be 1-5")
		return
	}

	exists, err := recipeExists(recipeID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "server error")
		return
	}
	if !exists {
		writeError(w, http.StatusNotFound, "recipe not found")
		return
	}

	tx, err := db.Begin()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "server error")
		return
	}
	defer tx.Rollback()

	if _, err := tx.Exec(`INSERT INTO ratings (recipe_id, rating) VALUES ($1,$2)`, recipeID, req.Rating); err != nil {
		writeError(w, http.StatusInternalServerError, "server error")
		return
	}
	if _, err := tx.Exec(
		`INSERT INTO recipe_stats (recipe_id, rating_sum, rating_count) VALUES ($1, $2, 1)
		 ON CONFLICT (recipe_id) DO UPDATE SET rating_sum = recipe_stats.rating_sum + EXCLUDED.rating_sum,
		 rating_count = recipe_stats.rating_count + 1`, recipeID, req.Rating); err != nil {
		writeError(w, http.StatusInternalServerError, "server error")
		return
	}
	if err := tx.Commit(); err != nil {
		writeError(w, http.StatusInternalServerError, "server error")
		return
	}
	pageCache.invalidate(recipeID)
	ovCache.Lock()
	ovCache.html = ""
	ovCache.Unlock()
	writeJSON(w, http.StatusCreated, map[string]string{"status": "created"})
}

func recipeExists(recipeID string) (bool, error) {
	if pageCache.isKnown(recipeID) {
		return true, nil
	}
	var exists bool
	if err := db.QueryRow(`SELECT EXISTS(SELECT 1 FROM recipes WHERE id=$1)`, recipeID).Scan(&exists); err != nil {
		return false, err
	}
	if exists {
		pageCache.markKnown(recipeID)
	}
	return exists, nil
}

func (c *recipePageCache) get(id string) (string, bool) {
	c.RLock()
	page, ok := c.pages[id]
	c.RUnlock()
	if !ok {
		return "", false
	}
	return page.html, true
}

func (c *recipePageCache) version(id string) uint64 {
	c.RLock()
	version := c.versions[id]
	c.RUnlock()
	return version
}

func (c *recipePageCache) store(id string, version uint64, html string) {
	c.Lock()
	c.ensureLocked()
	if c.versions[id] == version {
		c.pages[id] = cachedRecipePage{html: html, version: version}
		c.known[id] = struct{}{}
	}
	c.Unlock()
}

func (c *recipePageCache) invalidate(id string) {
	c.Lock()
	c.ensureLocked()
	c.versions[id]++
	delete(c.pages, id)
	c.known[id] = struct{}{}
	c.Unlock()
}

func (c *recipePageCache) markKnown(id string) {
	c.Lock()
	c.ensureLocked()
	c.known[id] = struct{}{}
	c.Unlock()
}

func (c *recipePageCache) isKnown(id string) bool {
	c.RLock()
	_, ok := c.known[id]
	c.RUnlock()
	return ok
}

// pqArray serializes Go []string into a PostgreSQL TEXT[] literal
type pqArray []string

func (a pqArray) Value() (driver.Value, error) {
	if len(a) == 0 {
		return "{}", nil
	}
	var sb strings.Builder
	sb.WriteByte('{')
	for i, s := range a {
		if i > 0 {
			sb.WriteByte(',')
		}
		sb.WriteByte('"')
		for _, r := range s {
			if r == '"' || r == '\\' {
				sb.WriteByte('\\')
			}
			sb.WriteRune(r)
		}
		sb.WriteByte('"')
	}
	sb.WriteByte('}')
	return sb.String(), nil
}

// stringArray scans PostgreSQL TEXT[] into Go []string
type stringArray []string

func (a *stringArray) Scan(src interface{}) error {
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
		return fmt.Errorf("unsupported type for stringArray: %T", src)
	}
	*a = parsePGArray(s)
	return nil
}

func parsePGArray(s string) []string {
	if len(s) < 2 || s[0] != '{' || s[len(s)-1] != '}' {
		return nil
	}
	s = s[1 : len(s)-1]
	if s == "" {
		return []string{}
	}
	var out []string
	var cur strings.Builder
	inQuotes := false
	i := 0
	hadAny := false
	for i < len(s) {
		c := s[i]
		hadAny = true
		if inQuotes {
			if c == '\\' && i+1 < len(s) {
				cur.WriteByte(s[i+1])
				i += 2
				continue
			}
			if c == '"' {
				inQuotes = false
				i++
				continue
			}
			cur.WriteByte(c)
			i++
		} else {
			if c == '"' {
				inQuotes = true
				i++
				continue
			}
			if c == ',' {
				out = append(out, cur.String())
				cur.Reset()
				i++
				continue
			}
			cur.WriteByte(c)
			i++
		}
	}
	if hadAny {
		out = append(out, cur.String())
	}
	return out
}

func main() {
	if err := initDB(); err != nil {
		log.Fatalf("db init failed: %v", err)
	}
	defer db.Close()

	mux := http.NewServeMux()
	mux.HandleFunc("/recipes", recipesHandler)
	mux.HandleFunc("/recipes/upload", uploadHandler)
	mux.HandleFunc("/recipes/", recipeDispatch)

	port := getEnv("PORT", "5001")
	addr := "0.0.0.0:" + port

	srv := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadTimeout:       15 * time.Second,
		ReadHeaderTimeout: 5 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       90 * time.Second,
	}

	log.Printf("Listening on %s", addr)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("server: %v", err)
	}
}
