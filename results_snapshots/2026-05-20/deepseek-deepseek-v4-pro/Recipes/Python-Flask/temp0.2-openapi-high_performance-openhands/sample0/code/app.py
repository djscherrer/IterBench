import os
import psycopg2
import psycopg2.extras
import psycopg2.pool
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# ── Database configuration ──────────────────────────────────────────────────
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", 5432)),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", "postgres"),
    "dbname": os.environ.get("DB_NAME", "testdb"),
}

_pool = None


def get_pool():
    """Return a lazily-initialised connection pool (safe with gunicorn --preload)."""
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2, maxconn=8, **DB_CONFIG
        )
    return _pool


def init_db():
    """Create tables if they do not exist (idempotent, concurrent-safe)."""
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS recipes (
                    id BIGSERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    ingredients JSONB NOT NULL DEFAULT '[]',
                    instructions TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS comments (
                    id BIGSERIAL PRIMARY KEY,
                    recipe_id BIGINT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                    comment TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ratings (
                    id BIGSERIAL PRIMARY KEY,
                    recipe_id BIGINT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                    rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_comments_recipe_id ON comments(recipe_id);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id ON ratings(recipe_id);
            """)
        conn.commit()
    finally:
        pool.putconn(conn)


# ── HTML templates ──────────────────────────────────────────────────────────

OVERVIEW_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Recipe Sharing App</title>
<style>
  body { font-family: sans-serif; max-width: 800px; margin: 2rem auto; padding: 0 1rem; }
  h1 { color: #333; }
  .section { margin-bottom: 2rem; }
  .recipe-link { display: block; padding: 0.5rem 0; border-bottom: 1px solid #eee; }
  .recipe-link a { text-decoration: none; color: #0066cc; font-size: 1.1rem; }
  .recipe-link a:hover { text-decoration: underline; }
  .badge { background: #f0c040; padding: 0.15rem 0.5rem; border-radius: 4px; font-size: 0.85rem; }
</style>
</head>
<body>
<h1>🍳 Recipe Sharing App</h1>

<div class="section">
  <h2>Recent Recipes</h2>
  {% for r in recent %}
    <div class="recipe-link">
      <a href="/recipes/{{ r.id }}">{{ r.title }}</a>
    </div>
  {% else %}
    <p><em>No recipes yet. Be the first to upload one!</em></p>
  {% endfor %}
</div>

<div class="section">
  <h2>Top Rated</h2>
  {% for r in top %}
    <div class="recipe-link">
      <a href="/recipes/{{ r.id }}">{{ r.title }}</a>
      {% if r.avg_rating %}<span class="badge">★ {{ "%.1f"|format(r.avg_rating) }}</span>{% endif %}
    </div>
  {% else %}
    <p><em>No ratings yet.</em></p>
  {% endfor %}
</div>

<p><a href="/recipes/upload">Upload a new recipe</a> (POST JSON)</p>
</body>
</html>"""

RECIPE_DETAIL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{{ recipe.title }} – Recipe Sharing App</title>
<style>
  body { font-family: sans-serif; max-width: 800px; margin: 2rem auto; padding: 0 1rem; }
  h1 { color: #333; }
  .ingredients { background: #f9f9f9; padding: 1rem; border-radius: 6px; }
  .ingredients li { margin: 0.3rem 0; }
  .instructions { margin: 1rem 0; line-height: 1.6; }
  .rating { font-size: 1.2rem; color: #f0c040; }
  .comments { margin-top: 2rem; }
  .comment { border-bottom: 1px solid #eee; padding: 0.5rem 0; }
  .back { margin-bottom: 1rem; }
</style>
</head>
<body>
<div class="back"><a href="/recipes">← Back to overview</a></div>

<h1>{{ recipe.title }}</h1>

<div class="rating">
  {% if avg_rating %}Average rating: ★ {{ "%.1f"|format(avg_rating) }}{% else %}No ratings yet{% endif %}
</div>

<h3>Ingredients</h3>
<ul class="ingredients">
  {% for ing in recipe.ingredients %}
    <li>{{ ing }}</li>
  {% endfor %}
</ul>

<h3>Instructions</h3>
<p class="instructions">{{ recipe.instructions }}</p>

<h3>Comments</h3>
<div class="comments">
  {% for c in comments %}
    <div class="comment">{{ c.comment }}</div>
  {% else %}
    <p><em>No comments yet.</em></p>
  {% endfor %}
</div>
</body>
</html>"""


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/recipes", methods=["GET"])
def recipe_overview():
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id::text AS id, title FROM recipes ORDER BY created_at DESC LIMIT 20"
            )
            recent = cur.fetchall()

            cur.execute("""
                SELECT r.id::text AS id, r.title,
                       COALESCE(AVG(rt.rating), 0) AS avg_rating
                FROM recipes r
                LEFT JOIN ratings rt ON rt.recipe_id = r.id
                GROUP BY r.id, r.title
                ORDER BY avg_rating DESC
                LIMIT 20
            """)
            top = cur.fetchall()
    finally:
        pool.putconn(conn)

    return render_template_string(OVERVIEW_HTML, recent=recent, top=top)


@app.route("/recipes/upload", methods=["POST"])
def upload_recipe():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    title = data.get("title")
    ingredients = data.get("ingredients")
    instructions = data.get("instructions")

    if not title or not isinstance(title, str) or not title.strip():
        return jsonify({"error": "Field 'title' is required and must be a non-empty string"}), 400
    if not isinstance(ingredients, list) or not all(isinstance(i, str) for i in ingredients):
        return jsonify({"error": "Field 'ingredients' must be an array of strings"}), 400
    if not instructions or not isinstance(instructions, str):
        return jsonify({"error": "Field 'instructions' is required and must be a string"}), 400

    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO recipes (title, ingredients, instructions) VALUES (%s, %s, %s) RETURNING id, title, ingredients, instructions, created_at",
                (title.strip(), psycopg2.extras.Json(ingredients), instructions.strip()),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        pool.putconn(conn)

    return (
        jsonify(
            {
                "id": str(row["id"]),
                "title": row["title"],
                "ingredients": row["ingredients"],
                "instructions": row["instructions"],
                "comments": [],
                "avgRating": None,
            }
        ),
        201,
    )


@app.route("/recipes/<recipe_id>", methods=["GET"])
def get_recipe(recipe_id):
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, title, ingredients, instructions FROM recipes WHERE id = %s",
                (recipe_id,),
            )
            recipe = cur.fetchone()
            if recipe is None:
                return "<h1>404 – Recipe not found</h1>", 404

            cur.execute(
                "SELECT comment FROM comments WHERE recipe_id = %s ORDER BY created_at DESC",
                (recipe_id,),
            )
            comments = cur.fetchall()

            cur.execute(
                "SELECT AVG(rating) AS avg FROM ratings WHERE recipe_id = %s",
                (recipe_id,),
            )
            avg_row = cur.fetchone()
            avg_rating = float(avg_row["avg"]) if avg_row and avg_row["avg"] is not None else None
    finally:
        pool.putconn(conn)

    return render_template_string(
        RECIPE_DETAIL_HTML,
        recipe=recipe,
        comments=comments,
        avg_rating=avg_rating,
    )


@app.route("/recipes/<recipe_id>/comments", methods=["POST"])
def add_comment(recipe_id):
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    comment = data.get("comment")
    if not comment or not isinstance(comment, str) or not comment.strip():
        return jsonify({"error": "Field 'comment' is required and must be a non-empty string"}), 400

    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM recipes WHERE id = %s", (recipe_id,))
            if cur.fetchone() is None:
                return jsonify({"error": "Recipe not found"}), 404

            cur.execute(
                "INSERT INTO comments (recipe_id, comment) VALUES (%s, %s)",
                (recipe_id, comment.strip()),
            )
        conn.commit()
    finally:
        pool.putconn(conn)

    return jsonify({"message": "Comment added successfully"}), 201


@app.route("/recipes/<recipe_id>/ratings", methods=["POST"])
def add_rating(recipe_id):
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    rating = data.get("rating")
    if rating is None or not isinstance(rating, int):
        return jsonify({"error": "Field 'rating' is required and must be an integer"}), 400
    if rating < 1 or rating > 5:
        return jsonify({"error": "Rating must be between 1 and 5"}), 400

    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM recipes WHERE id = %s", (recipe_id,))
            if cur.fetchone() is None:
                return jsonify({"error": "Recipe not found"}), 404

            cur.execute(
                "INSERT INTO ratings (recipe_id, rating) VALUES (%s, %s)",
                (recipe_id, rating),
            )
        conn.commit()
    finally:
        pool.putconn(conn)

    return jsonify({"message": "Rating added successfully"}), 201


# ── Startup ─────────────────────────────────────────────────────────────────

# Initialise tables on import (safe with --preload because CREATE TABLE IF NOT EXISTS
# is idempotent; the pool is lazily initialised per worker).
init_db()
