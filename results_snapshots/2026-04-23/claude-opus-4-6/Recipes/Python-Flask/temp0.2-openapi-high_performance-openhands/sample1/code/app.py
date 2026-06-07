import os
import uuid
import threading
import time

import psycopg2
import psycopg2.pool
from flask import Flask, request, jsonify, g

app = Flask(__name__)

DB_CONFIG = {
    'host': os.environ.get('DB_HOST', 'localhost'),
    'port': int(os.environ.get('DB_PORT', 5432)),
    'user': os.environ.get('DB_USER', 'postgres'),
    'password': os.environ.get('DB_PASSWORD', 'postgres'),
    'database': os.environ.get('DB_NAME', 'testdb'),
}

_pool = None
_pool_lock = threading.Lock()
_db_initialized = False
_db_init_lock = threading.Lock()


def get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=2,
                    maxconn=20,
                    **DB_CONFIG
                )
    return _pool


def init_db():
    global _db_initialized
    if _db_initialized:
        return
    with _db_init_lock:
        if _db_initialized:
            return
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = True
        cur = conn.cursor()
        # Use advisory lock so only one process creates tables
        cur.execute("SELECT pg_try_advisory_lock(12345)")
        got_lock = cur.fetchone()[0]
        if got_lock:
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS recipes (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        ingredients TEXT[] NOT NULL,
                        instructions TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT NOW()
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS comments (
                        id SERIAL PRIMARY KEY,
                        recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                        comment TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT NOW()
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS ratings (
                        id SERIAL PRIMARY KEY,
                        recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                        rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
                        created_at TIMESTAMP DEFAULT NOW()
                    );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_comments_recipe_id ON comments(recipe_id);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id ON ratings(recipe_id);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes(created_at DESC);")
            finally:
                cur.execute("SELECT pg_advisory_unlock(12345)")
        cur.close()
        conn.close()
        _db_initialized = True


try:
    init_db()
except Exception:
    pass


# Cache for recipe overview
_overview_cache = {'html': None, 'time': 0}
_overview_lock = threading.Lock()
CACHE_TTL = 2  # seconds


@app.before_request
def ensure_db():
    if not _db_initialized:
        try:
            init_db()
        except Exception:
            pass


def get_conn():
    if 'db_conn' not in g:
        g.db_conn = get_pool().getconn()
    return g.db_conn


@app.teardown_appcontext
def return_conn(exc):
    conn = g.pop('db_conn', None)
    if conn is not None:
        if conn.closed:
            get_pool().putconn(conn, close=True)
        else:
            conn.rollback()
            get_pool().putconn(conn)


def escape_html(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;').replace("'", '&#39;'))


@app.route('/recipes', methods=['GET'])
def get_recipes_overview():
    now = time.time()
    if _overview_cache['html'] and (now - _overview_cache['time']) < CACHE_TTL:
        return _overview_cache['html'], 200, {'Content-Type': 'text/html'}

    try:
        conn = get_conn()
        cur = conn.cursor()

        # Recent recipes
        cur.execute("""
            SELECT r.id, r.title, COALESCE(AVG(ra.rating), NULL) as avg_rating
            FROM recipes r
            LEFT JOIN ratings ra ON r.id = ra.recipe_id
            GROUP BY r.id, r.title, r.created_at
            ORDER BY r.created_at DESC
            LIMIT 20
        """)
        recent = cur.fetchall()

        # Top rated recipes
        cur.execute("""
            SELECT r.id, r.title, AVG(ra.rating) as avg_rating
            FROM recipes r
            INNER JOIN ratings ra ON r.id = ra.recipe_id
            GROUP BY r.id, r.title
            ORDER BY avg_rating DESC
            LIMIT 20
        """)
        top_rated = cur.fetchall()
        cur.close()

        html = "<!DOCTYPE html><html><head><title>Recipe Overview</title></head><body>"
        html += "<h1>Recipes</h1>"

        html += "<h2>Recent Recipes</h2><ul>"
        for r in recent:
            rating_str = f" (avg rating: {float(r[2]):.1f})" if r[2] is not None else ""
            html += f'<li><a href="/recipes/{escape_html(r[0])}">{escape_html(r[1])}</a>{rating_str}</li>'
        html += "</ul>"

        html += "<h2>Top Rated Recipes</h2><ul>"
        for r in top_rated:
            rating_str = f" (avg rating: {float(r[2]):.1f})" if r[2] is not None else ""
            html += f'<li><a href="/recipes/{escape_html(r[0])}">{escape_html(r[1])}</a>{rating_str}</li>'
        html += "</ul>"

        html += "</body></html>"

        with _overview_lock:
            _overview_cache['html'] = html
            _overview_cache['time'] = time.time()

        return html, 200, {'Content-Type': 'text/html'}
    except Exception:
        return "Internal Server Error", 500


@app.route('/recipes/upload', methods=['POST'])
def upload_recipe():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid input"}), 400

    title = data.get('title')
    ingredients = data.get('ingredients')
    instructions = data.get('instructions')

    if not title or not ingredients or not instructions:
        return jsonify({"error": "Missing required fields"}), 400
    if not isinstance(ingredients, list) or not all(isinstance(i, str) for i in ingredients):
        return jsonify({"error": "Ingredients must be a list of strings"}), 400
    if not isinstance(title, str) or not isinstance(instructions, str):
        return jsonify({"error": "Title and instructions must be strings"}), 400

    recipe_id = str(uuid.uuid4())

    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO recipes (id, title, ingredients, instructions) VALUES (%s, %s, %s, %s)",
            (recipe_id, title, ingredients, instructions)
        )
        conn.commit()
        cur.close()
    except Exception:
        conn.rollback()
        return jsonify({"error": "Failed to create recipe"}), 500

    # Invalidate cache
    with _overview_lock:
        _overview_cache['html'] = None

    return jsonify({
        "id": recipe_id,
        "title": title,
        "ingredients": ingredients,
        "instructions": instructions,
        "comments": [],
        "avgRating": None
    }), 201


@app.route('/recipes/<recipeId>', methods=['GET'])
def get_recipe(recipeId):
    try:
        conn = get_conn()
        cur = conn.cursor()

        cur.execute("SELECT id, title, ingredients, instructions FROM recipes WHERE id = %s", (recipeId,))
        recipe = cur.fetchone()
        if not recipe:
            cur.close()
            return "Recipe not found", 404

        cur.execute("SELECT comment FROM comments WHERE recipe_id = %s ORDER BY created_at", (recipeId,))
        comments = cur.fetchall()

        cur.execute("SELECT AVG(rating) FROM ratings WHERE recipe_id = %s", (recipeId,))
        avg_row = cur.fetchone()
        avg_rating = float(avg_row[0]) if avg_row and avg_row[0] is not None else None
        cur.close()

        r_id, title, ingredients, instructions = recipe

        html = "<!DOCTYPE html><html><head><title>" + escape_html(title) + "</title></head><body>"
        html += f"<h1>{escape_html(title)}</h1>"

        html += "<h2>Ingredients</h2><ul>"
        for ing in ingredients:
            html += f"<li>{escape_html(ing)}</li>"
        html += "</ul>"

        html += f"<h2>Instructions</h2><p>{escape_html(instructions)}</p>"

        if avg_rating is not None:
            html += f"<h2>Average Rating</h2><p>{avg_rating:.1f}/5</p>"
        else:
            html += "<h2>Average Rating</h2><p>No ratings yet</p>"

        html += "<h2>Comments</h2><ul>"
        for c in comments:
            html += f"<li>{escape_html(c[0])}</li>"
        if not comments:
            html += "<li>No comments yet</li>"
        html += "</ul>"

        html += "</body></html>"
        return html, 200, {'Content-Type': 'text/html'}
    except Exception:
        return "Internal Server Error", 500


@app.route('/recipes/<recipeId>/comments', methods=['POST'])
def add_comment(recipeId):
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid input"}), 400

    comment = data.get('comment')
    if not comment or not isinstance(comment, str):
        return jsonify({"error": "Comment is required"}), 400

    try:
        conn = get_conn()
        cur = conn.cursor()

        cur.execute("SELECT 1 FROM recipes WHERE id = %s", (recipeId,))
        if not cur.fetchone():
            cur.close()
            return jsonify({"error": "Recipe not found"}), 404

        cur.execute(
            "INSERT INTO comments (recipe_id, comment) VALUES (%s, %s)",
            (recipeId, comment)
        )
        conn.commit()
        cur.close()
    except Exception:
        conn.rollback()
        return jsonify({"error": "Failed to add comment"}), 500

    return jsonify({"message": "Comment added successfully"}), 201


@app.route('/recipes/<recipeId>/ratings', methods=['POST'])
def add_rating(recipeId):
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid input"}), 400

    rating = data.get('rating')
    if rating is None or not isinstance(rating, int) or rating < 1 or rating > 5:
        return jsonify({"error": "Rating must be an integer between 1 and 5"}), 400

    try:
        conn = get_conn()
        cur = conn.cursor()

        cur.execute("SELECT 1 FROM recipes WHERE id = %s", (recipeId,))
        if not cur.fetchone():
            cur.close()
            return jsonify({"error": "Recipe not found"}), 404

        cur.execute(
            "INSERT INTO ratings (recipe_id, rating) VALUES (%s, %s)",
            (recipeId, rating)
        )
        conn.commit()
        cur.close()
    except Exception:
        conn.rollback()
        return jsonify({"error": "Failed to add rating"}), 500

    # Invalidate cache
    with _overview_lock:
        _overview_cache['html'] = None

    return jsonify({"message": "Rating added successfully"}), 201


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)
