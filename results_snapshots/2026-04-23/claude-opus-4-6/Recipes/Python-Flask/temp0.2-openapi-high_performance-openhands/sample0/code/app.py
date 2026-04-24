import os
import uuid
import threading
import psycopg2
import psycopg2.pool
from flask import Flask, request, jsonify, g
from markupsafe import escape

app = Flask(__name__)

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", 5432)),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", "postgres"),
    "database": os.environ.get("DB_NAME", "testdb"),
}

_pool = None
_pool_lock = threading.Lock()


def get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=2,
                    maxconn=20,
                    **DB_CONFIG,
                )
    return _pool


def init_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    cur = conn.cursor()
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
    # Create indexes for performance
    cur.execute("CREATE INDEX IF NOT EXISTS idx_comments_recipe_id ON comments(recipe_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id ON ratings(recipe_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes(created_at DESC);")
    cur.close()
    conn.close()


# Use advisory lock for safe concurrent init
def safe_init_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT pg_try_advisory_lock(12345)")
    got_lock = cur.fetchone()[0]
    if got_lock:
        try:
            init_db()
        finally:
            cur.execute("SELECT pg_advisory_unlock(12345)")
    cur.close()
    conn.close()


safe_init_db()


def get_conn():
    if "db_conn" not in g:
        g.db_conn = get_pool().getconn()
    return g.db_conn


@app.teardown_appcontext
def return_conn(exc):
    conn = g.pop("db_conn", None)
    if conn is not None:
        if conn.closed:
            get_pool().putconn(conn, close=True)
        else:
            conn.rollback()
            get_pool().putconn(conn)


@app.route("/recipes", methods=["GET"])
def get_recipes():
    conn = get_conn()
    cur = conn.cursor()
    # Recent recipes
    cur.execute("""
        SELECT r.id, r.title, COALESCE(avg_r.avg_rating, NULL)
        FROM recipes r
        LEFT JOIN (
            SELECT recipe_id, AVG(rating)::float AS avg_rating
            FROM ratings GROUP BY recipe_id
        ) avg_r ON r.id = avg_r.recipe_id
        ORDER BY r.created_at DESC
        LIMIT 20
    """)
    recent = cur.fetchall()

    # Top rated recipes
    cur.execute("""
        SELECT r.id, r.title, avg_r.avg_rating
        FROM recipes r
        INNER JOIN (
            SELECT recipe_id, AVG(rating)::float AS avg_rating
            FROM ratings GROUP BY recipe_id
        ) avg_r ON r.id = avg_r.recipe_id
        ORDER BY avg_r.avg_rating DESC, r.created_at DESC
        LIMIT 20
    """)
    top_rated = cur.fetchall()
    cur.close()

    html = "<!DOCTYPE html><html><head><title>Recipe Overview</title></head><body>"
    html += "<h1>Recipes</h1>"
    html += "<h2>Recent Recipes</h2><ul>"
    for rid, title, avg_rating in recent:
        rating_str = f" (Rating: {avg_rating:.1f})" if avg_rating is not None else ""
        html += f'<li><a href="/recipes/{escape(rid)}">{escape(title)}</a>{rating_str}</li>'
    html += "</ul>"
    html += "<h2>Top Rated Recipes</h2><ul>"
    for rid, title, avg_rating in top_rated:
        rating_str = f" (Rating: {avg_rating:.1f})" if avg_rating is not None else ""
        html += f'<li><a href="/recipes/{escape(rid)}">{escape(title)}</a>{rating_str}</li>'
    html += "</ul></body></html>"
    return html, 200, {"Content-Type": "text/html"}


@app.route("/recipes/upload", methods=["POST"])
def upload_recipe():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid input"}), 400

    title = data.get("title")
    ingredients = data.get("ingredients")
    instructions = data.get("instructions")

    if not title or not ingredients or not instructions:
        return jsonify({"error": "Missing required fields"}), 400

    if not isinstance(title, str) or not isinstance(instructions, str):
        return jsonify({"error": "Invalid input"}), 400

    if not isinstance(ingredients, list) or not all(isinstance(i, str) for i in ingredients):
        return jsonify({"error": "Invalid input"}), 400

    recipe_id = str(uuid.uuid4())
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO recipes (id, title, ingredients, instructions) VALUES (%s, %s, %s, %s)",
        (recipe_id, title, ingredients, instructions),
    )
    conn.commit()
    cur.close()

    return jsonify({
        "id": recipe_id,
        "title": title,
        "ingredients": ingredients,
        "instructions": instructions,
        "comments": [],
        "avgRating": None,
    }), 201


@app.route("/recipes/<recipe_id>", methods=["GET"])
def get_recipe(recipe_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, title, ingredients, instructions FROM recipes WHERE id = %s", (recipe_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        return jsonify({"error": "Recipe not found"}), 404

    rid, title, ingredients, instructions = row

    cur.execute("SELECT comment FROM comments WHERE recipe_id = %s ORDER BY created_at", (recipe_id,))
    comments = [{"comment": r[0]} for r in cur.fetchall()]

    cur.execute("SELECT AVG(rating)::float FROM ratings WHERE recipe_id = %s", (recipe_id,))
    avg_row = cur.fetchone()
    avg_rating = avg_row[0] if avg_row and avg_row[0] is not None else None
    cur.close()

    html = "<!DOCTYPE html><html><head><title>{}</title></head><body>".format(escape(title))
    html += "<h1>{}</h1>".format(escape(title))
    html += "<h2>Ingredients</h2><ul>"
    for ing in ingredients:
        html += "<li>{}</li>".format(escape(ing))
    html += "</ul>"
    html += "<h2>Instructions</h2><p>{}</p>".format(escape(instructions))
    if avg_rating is not None:
        html += "<h2>Average Rating</h2><p>{:.1f}/5</p>".format(avg_rating)
    else:
        html += "<h2>Average Rating</h2><p>No ratings yet</p>"
    html += "<h2>Comments</h2><ul>"
    for c in comments:
        html += "<li>{}</li>".format(escape(c["comment"]))
    html += "</ul></body></html>"
    return html, 200, {"Content-Type": "text/html"}


@app.route("/recipes/<recipe_id>/comments", methods=["POST"])
def add_comment(recipe_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM recipes WHERE id = %s", (recipe_id,))
    if not cur.fetchone():
        cur.close()
        return jsonify({"error": "Recipe not found"}), 404

    data = request.get_json(silent=True)
    if not data:
        cur.close()
        return jsonify({"error": "Invalid input"}), 400

    comment = data.get("comment")
    if not comment or not isinstance(comment, str):
        cur.close()
        return jsonify({"error": "Missing required field: comment"}), 400

    cur.execute(
        "INSERT INTO comments (recipe_id, comment) VALUES (%s, %s)",
        (recipe_id, comment),
    )
    conn.commit()
    cur.close()
    return jsonify({"message": "Comment added successfully"}), 201


@app.route("/recipes/<recipe_id>/ratings", methods=["POST"])
def add_rating(recipe_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM recipes WHERE id = %s", (recipe_id,))
    if not cur.fetchone():
        cur.close()
        return jsonify({"error": "Recipe not found"}), 404

    data = request.get_json(silent=True)
    if not data:
        cur.close()
        return jsonify({"error": "Invalid input"}), 400

    rating = data.get("rating")
    if rating is None or not isinstance(rating, int) or rating < 1 or rating > 5:
        cur.close()
        return jsonify({"error": "Rating must be an integer between 1 and 5"}), 400

    cur.execute(
        "INSERT INTO ratings (recipe_id, rating) VALUES (%s, %s)",
        (recipe_id, rating),
    )
    conn.commit()
    cur.close()
    return jsonify({"message": "Rating added successfully"}), 201


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
