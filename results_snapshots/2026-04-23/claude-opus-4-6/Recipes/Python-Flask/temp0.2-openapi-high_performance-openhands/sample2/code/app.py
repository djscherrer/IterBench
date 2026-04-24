import os
import threading
from html import escape as html_escape
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

db_pool = None
_pool_lock = threading.Lock()


def get_pool():
    global db_pool
    if db_pool is None:
        with _pool_lock:
            if db_pool is None:
                db_pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=2,
                    maxconn=20,
                    **DB_CONFIG
                )
    return db_pool


def init_db():
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS recipes (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                ingredients TEXT[] NOT NULL,
                instructions TEXT NOT NULL,
                avg_rating DOUBLE PRECISION,
                rating_count INTEGER NOT NULL DEFAULT 0,
                rating_sum INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id SERIAL PRIMARY KEY,
                recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                comment TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                id SERIAL PRIMARY KEY,
                recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        # Create indexes for performance
        cur.execute("CREATE INDEX IF NOT EXISTS idx_comments_recipe_id ON comments(recipe_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id ON ratings(recipe_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_recipes_avg_rating ON recipes(avg_rating DESC NULLS LAST);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes(created_at DESC);")
        cur.close()
        conn.close()
    except Exception:
        if conn:
            conn.close()
        raise


def get_db():
    if 'db' not in g:
        g.db = get_pool().getconn()
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        if exc:
            db.rollback()
        get_pool().putconn(db)


# Initialize DB tables on startup (safe for concurrent workers with IF NOT EXISTS)
init_db()


@app.route('/recipes', methods=['GET'])
def get_recipes():
    conn = get_db()
    cur = conn.cursor()
    try:
        # Recent recipes
        cur.execute("SELECT id, title FROM recipes ORDER BY created_at DESC LIMIT 20;")
        recent = cur.fetchall()

        # Top rated recipes
        cur.execute("SELECT id, title, avg_rating FROM recipes WHERE avg_rating IS NOT NULL ORDER BY avg_rating DESC LIMIT 20;")
        top_rated = cur.fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        return "Server error", 500

    html = "<!DOCTYPE html><html><head><title>Recipes</title></head><body>"
    html += "<h1>Recipes</h1>"

    html += "<h2>Recent Recipes</h2><ul>"
    for r in recent:
        html += f'<li><a href="/recipes/{r[0]}">{html_escape(r[1])}</a></li>'
    html += "</ul>"

    html += "<h2>Top Rated Recipes</h2><ul>"
    for r in top_rated:
        rating_str = f" ({r[2]:.1f})" if r[2] is not None else ""
        html += f'<li><a href="/recipes/{r[0]}">{html_escape(r[1])}</a>{rating_str}</li>'
    html += "</ul>"

    html += "</body></html>"
    return html, 200, {'Content-Type': 'text/html'}


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

    if not isinstance(title, str) or not isinstance(ingredients, list) or not isinstance(instructions, str):
        return jsonify({"error": "Invalid input types"}), 400

    if not all(isinstance(i, str) for i in ingredients):
        return jsonify({"error": "Ingredients must be strings"}), 400

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO recipes (title, ingredients, instructions) VALUES (%s, %s, %s) RETURNING id;",
            (title, ingredients, instructions)
        )
        recipe_id = cur.fetchone()[0]
        conn.commit()
    except Exception:
        conn.rollback()
        return jsonify({"error": "Server error"}), 500

    return jsonify({
        "id": str(recipe_id),
        "title": title,
        "ingredients": ingredients,
        "instructions": instructions,
        "comments": [],
        "avgRating": None
    }), 201


@app.route('/recipes/<recipeId>', methods=['GET'])
def get_recipe(recipeId):
    try:
        rid = int(recipeId)
    except (ValueError, TypeError):
        return "Recipe not found", 404

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, title, ingredients, instructions, avg_rating FROM recipes WHERE id = %s;", (rid,))
        recipe = cur.fetchone()
        if not recipe:
            conn.commit()
            return "Recipe not found", 404

        cur.execute("SELECT comment FROM comments WHERE recipe_id = %s ORDER BY created_at;", (rid,))
        comments = cur.fetchall()

        cur.execute("SELECT rating FROM ratings WHERE recipe_id = %s;", (rid,))
        ratings_rows = cur.fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        return "Server error", 500

    r_id, title, ingredients, instructions, avg_rating = recipe

    html = "<!DOCTYPE html><html><head><title>" + html_escape(title) + "</title></head><body>"
    html += f"<h1>{html_escape(title)}</h1>"

    html += "<h2>Ingredients</h2><ul>"
    for ing in ingredients:
        html += f"<li>{html_escape(ing)}</li>"
    html += "</ul>"

    html += f"<h2>Instructions</h2><p>{html_escape(instructions)}</p>"

    if avg_rating is not None:
        html += f"<h2>Average Rating</h2><p>{avg_rating:.1f} ({len(ratings_rows)} ratings)</p>"
    else:
        html += "<h2>Average Rating</h2><p>No ratings yet</p>"

    html += "<h2>Comments</h2><ul>"
    for c in comments:
        html += f"<li>{html_escape(c[0])}</li>"
    html += "</ul>"

    html += "</body></html>"
    return html, 200, {'Content-Type': 'text/html'}


@app.route('/recipes/<recipeId>/comments', methods=['POST'])
def add_comment(recipeId):
    try:
        rid = int(recipeId)
    except (ValueError, TypeError):
        return jsonify({"error": "Recipe not found"}), 404

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid input"}), 400

    comment = data.get('comment')
    if not comment or not isinstance(comment, str):
        return jsonify({"error": "Missing or invalid comment"}), 400

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM recipes WHERE id = %s;", (rid,))
        if not cur.fetchone():
            conn.commit()
            return jsonify({"error": "Recipe not found"}), 404

        cur.execute(
            "INSERT INTO comments (recipe_id, comment) VALUES (%s, %s);",
            (rid, comment)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        return jsonify({"error": "Server error"}), 500

    return jsonify({"message": "Comment added successfully"}), 201


@app.route('/recipes/<recipeId>/ratings', methods=['POST'])
def add_rating(recipeId):
    try:
        rid = int(recipeId)
    except (ValueError, TypeError):
        return jsonify({"error": "Recipe not found"}), 404

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid input"}), 400

    rating = data.get('rating')
    if rating is None or not isinstance(rating, int) or rating < 1 or rating > 5:
        return jsonify({"error": "Rating must be an integer between 1 and 5"}), 400

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM recipes WHERE id = %s;", (rid,))
        if not cur.fetchone():
            conn.commit()
            return jsonify({"error": "Recipe not found"}), 404

        cur.execute(
            "INSERT INTO ratings (recipe_id, rating) VALUES (%s, %s);",
            (rid, rating)
        )
        # Update denormalized avg_rating on the recipe
        cur.execute(
            """UPDATE recipes
               SET rating_count = rating_count + 1,
                   rating_sum = rating_sum + %s,
                   avg_rating = (rating_sum + %s)::DOUBLE PRECISION / (rating_count + 1)
               WHERE id = %s;""",
            (rating, rating, rid)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        return jsonify({"error": "Server error"}), 500

    return jsonify({"message": "Rating added successfully"}), 201


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)
