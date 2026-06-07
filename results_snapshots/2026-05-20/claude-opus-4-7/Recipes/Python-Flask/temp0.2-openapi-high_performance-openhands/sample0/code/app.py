import os
import json
import uuid
import logging
from flask import Flask, request, jsonify, Response
from markupsafe import escape
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "postgres")
DB_NAME = os.environ.get("DB_NAME", "testdb")

# Connection pool - created lazily per-worker to be safe with preload+fork
_pool = None


def get_pool():
    global _pool
    if _pool is None:
        _pool = pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=20,
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            dbname=DB_NAME,
        )
    return _pool


class DBConn:
    def __init__(self):
        self.conn = None

    def __enter__(self):
        self.conn = get_pool().getconn()
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn is not None:
            try:
                if exc_type is not None:
                    self.conn.rollback()
            except Exception:
                pass
            get_pool().putconn(self.conn)


def init_db():
    """Initialize the database schema. Safe for concurrent execution."""
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname=DB_NAME,
    )
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(727272)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS recipes (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    ingredients JSONB NOT NULL,
                    instructions TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS comments (
                    id BIGSERIAL PRIMARY KEY,
                    recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                    comment TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ratings (
                    id BIGSERIAL PRIMARY KEY,
                    recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                    rating SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_comments_recipe ON comments(recipe_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ratings_recipe ON ratings(recipe_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_recipes_created ON recipes(created_at DESC)")
            cur.execute("SELECT pg_advisory_unlock(727272)")
    finally:
        conn.close()


try:
    init_db()
except Exception as e:
    log.exception("Database initialization failed: %s", e)


def fetch_recipe(recipe_id):
    with DBConn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, title, ingredients, instructions FROM recipes WHERE id = %s",
                (recipe_id,),
            )
            r = cur.fetchone()
            if not r:
                return None
            cur.execute(
                "SELECT comment FROM comments WHERE recipe_id = %s ORDER BY id ASC",
                (recipe_id,),
            )
            comments = [{"comment": row["comment"]} for row in cur.fetchall()]
            cur.execute(
                "SELECT AVG(rating)::float AS avg FROM ratings WHERE recipe_id = %s",
                (recipe_id,),
            )
            avg_row = cur.fetchone()
            avg = avg_row["avg"] if avg_row and avg_row["avg"] is not None else None
            ingredients = r["ingredients"]
            if isinstance(ingredients, str):
                try:
                    ingredients = json.loads(ingredients)
                except Exception:
                    ingredients = []
            return {
                "id": r["id"],
                "title": r["title"],
                "ingredients": ingredients,
                "instructions": r["instructions"],
                "comments": comments,
                "avgRating": avg,
            }


@app.route("/recipes", methods=["GET"])
def get_recipes_overview():
    try:
        with DBConn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT r.id, r.title, AVG(rt.rating)::float AS avg_rating
                    FROM recipes r
                    LEFT JOIN ratings rt ON rt.recipe_id = r.id
                    GROUP BY r.id, r.title, r.created_at
                    ORDER BY r.created_at DESC
                    LIMIT 50
                """)
                recent = cur.fetchall()
                cur.execute("""
                    SELECT r.id, r.title, AVG(rt.rating)::float AS avg_rating
                    FROM recipes r
                    JOIN ratings rt ON rt.recipe_id = r.id
                    GROUP BY r.id, r.title
                    ORDER BY AVG(rt.rating) DESC NULLS LAST
                    LIMIT 10
                """)
                top = cur.fetchall()

        def render_list(items):
            parts = []
            for it in items:
                rid = escape(it["id"])
                title = escape(it["title"])
                avg = it["avg_rating"]
                if avg is not None:
                    parts.append(
                        f'<li><a href="/recipes/{rid}">{title}</a> (avg rating: {avg:.2f})</li>'
                    )
                else:
                    parts.append(f'<li><a href="/recipes/{rid}">{title}</a></li>')
            return "\n".join(parts) if parts else "<li>No recipes yet</li>"

        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Recipes</title></head>
<body>
<h1>Recipes Overview</h1>
<h2>Recent Recipes</h2>
<ul>{render_list(recent)}</ul>
<h2>Top Rated Recipes</h2>
<ul>{render_list(top)}</ul>
</body></html>"""
        return Response(html, mimetype="text/html", status=200)
    except Exception:
        log.exception("Error in /recipes overview")
        return Response("Server error", status=500)


@app.route("/recipes/upload", methods=["POST"])
def upload_recipe():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid input"}), 400
    title = data.get("title")
    ingredients = data.get("ingredients")
    instructions = data.get("instructions")
    if not isinstance(title, str) or not title.strip():
        return jsonify({"error": "Invalid input"}), 400
    if not isinstance(ingredients, list) or not all(isinstance(i, str) for i in ingredients):
        return jsonify({"error": "Invalid input"}), 400
    if not isinstance(instructions, str) or not instructions.strip():
        return jsonify({"error": "Invalid input"}), 400

    recipe_id = uuid.uuid4().hex
    try:
        with DBConn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO recipes (id, title, ingredients, instructions) VALUES (%s, %s, %s::jsonb, %s)",
                    (recipe_id, title, json.dumps(ingredients), instructions),
                )
            conn.commit()
    except Exception:
        log.exception("Error inserting recipe")
        return jsonify({"error": "Server error"}), 500

    return jsonify({
        "id": recipe_id,
        "title": title,
        "ingredients": ingredients,
        "instructions": instructions,
        "comments": [],
        "avgRating": None,
    }), 201


@app.route("/recipes/<recipeId>", methods=["GET"])
def get_recipe(recipeId):
    try:
        recipe = fetch_recipe(recipeId)
    except Exception:
        log.exception("Error fetching recipe")
        return Response("Server error", status=500)
    if not recipe:
        return Response("Recipe not found", status=404)

    ingredients_html = "".join(f"<li>{escape(i)}</li>" for i in recipe["ingredients"])
    comments_html = "".join(f"<li>{escape(c['comment'])}</li>" for c in recipe["comments"])
    avg = recipe["avgRating"]
    avg_str = f"{avg:.2f}" if avg is not None else "No ratings yet"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{escape(recipe['title'])}</title></head>
<body>
<h1>{escape(recipe['title'])}</h1>
<h2>Ingredients</h2>
<ul>{ingredients_html}</ul>
<h2>Instructions</h2>
<p>{escape(recipe['instructions'])}</p>
<h2>Average Rating</h2>
<p>{avg_str}</p>
<h2>Comments</h2>
<ul>{comments_html}</ul>
</body></html>"""
    return Response(html, mimetype="text/html", status=200)


@app.route("/recipes/<recipeId>/comments", methods=["POST"])
def add_comment(recipeId):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid input"}), 400
    comment = data.get("comment")
    if not isinstance(comment, str) or not comment.strip():
        return jsonify({"error": "Invalid input"}), 400
    try:
        with DBConn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM recipes WHERE id = %s", (recipeId,))
                if not cur.fetchone():
                    return jsonify({"error": "Recipe not found"}), 404
                cur.execute(
                    "INSERT INTO comments (recipe_id, comment) VALUES (%s, %s)",
                    (recipeId, comment),
                )
            conn.commit()
    except Exception:
        log.exception("Error adding comment")
        return jsonify({"error": "Server error"}), 500
    return jsonify({"message": "Comment added successfully"}), 201


@app.route("/recipes/<recipeId>/ratings", methods=["POST"])
def add_rating(recipeId):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid input"}), 400
    rating = data.get("rating")
    if isinstance(rating, bool) or not isinstance(rating, int) or rating < 1 or rating > 5:
        return jsonify({"error": "Invalid input"}), 400
    try:
        with DBConn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM recipes WHERE id = %s", (recipeId,))
                if not cur.fetchone():
                    return jsonify({"error": "Recipe not found"}), 404
                cur.execute(
                    "INSERT INTO ratings (recipe_id, rating) VALUES (%s, %s)",
                    (recipeId, rating),
                )
            conn.commit()
    except Exception:
        log.exception("Error adding rating")
        return jsonify({"error": "Server error"}), 500
    return jsonify({"message": "Rating added successfully"}), 201


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "Method not allowed"}), 405


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port)
