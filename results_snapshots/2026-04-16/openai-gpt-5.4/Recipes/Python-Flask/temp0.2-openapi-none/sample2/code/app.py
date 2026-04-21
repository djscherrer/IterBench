import os
import uuid
from contextlib import contextmanager

import psycopg2
from psycopg2 import pool
from flask import Flask, jsonify, request, abort, Response

app = Flask(__name__)

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "postgres")
PORT = int(os.environ.get("PORT", "5001"))

db_pool = None


def get_dsn(dbname=None):
    return {
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "dbname": dbname or DB_NAME,
    }


def ensure_database_exists():
    conn = psycopg2.connect(**get_dsn("postgres"))
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
            exists = cur.fetchone() is not None
            if not exists:
                cur.execute(f'CREATE DATABASE "{DB_NAME}"')
    finally:
        conn.close()


def init_pool():
    global db_pool
    if db_pool is None:
        db_pool = psycopg2.pool.SimpleConnectionPool(
            minconn=1,
            maxconn=10,
            **get_dsn()
        )


@contextmanager
def get_conn():
    conn = db_pool.getconn()
    try:
        yield conn
    finally:
        db_pool.putconn(conn)


def init_db():
    ensure_database_exists()
    init_pool()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS recipes (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS recipe_ingredients (
                    id BIGSERIAL PRIMARY KEY,
                    recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                    ingredient TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS recipe_comments (
                    id BIGSERIAL PRIMARY KEY,
                    recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                    comment TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS recipe_ratings (
                    id BIGSERIAL PRIMARY KEY,
                    recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                    rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_recipe_id ON recipe_ingredients(recipe_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_recipe_comments_recipe_id ON recipe_comments(recipe_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_recipe_ratings_recipe_id ON recipe_ratings(recipe_id)"
            )
        conn.commit()


def recipe_exists(recipe_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM recipes WHERE id = %s", (recipe_id,))
            return cur.fetchone() is not None


def fetch_recipe(recipe_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    r.id,
                    r.title,
                    r.instructions,
                    AVG(rr.rating)::float
                FROM recipes r
                LEFT JOIN recipe_ratings rr ON rr.recipe_id = r.id
                WHERE r.id = %s
                GROUP BY r.id, r.title, r.instructions
                """,
                (recipe_id,),
            )
            recipe_row = cur.fetchone()
            if not recipe_row:
                return None

            cur.execute(
                """
                SELECT ingredient
                FROM recipe_ingredients
                WHERE recipe_id = %s
                ORDER BY id ASC
                """,
                (recipe_id,),
            )
            ingredients = [row[0] for row in cur.fetchall()]

            cur.execute(
                """
                SELECT comment
                FROM recipe_comments
                WHERE recipe_id = %s
                ORDER BY id ASC
                """,
                (recipe_id,),
            )
            comments = [{"comment": row[0]} for row in cur.fetchall()]

            return {
                "id": recipe_row[0],
                "title": recipe_row[1],
                "ingredients": ingredients,
                "instructions": recipe_row[2],
                "comments": comments,
                "avgRating": recipe_row[3],
            }


def render_recipe_overview_html(recipes):
    html = [
        "<!DOCTYPE html>",
        "<html>",
        "<head><title>Recipe Overview</title></head>",
        "<body>",
        "<h1>Recipe Overview</h1>",
        "<ul>",
    ]
    for recipe in recipes:
        title = escape_html(recipe["title"])
        recipe_id = escape_html(recipe["id"])
        avg_rating = recipe["avg_rating"]
        rating_text = "No ratings" if avg_rating is None else f"Avg rating: {avg_rating:.2f}"
        html.append(
            f'<li><a href="/recipes/{recipe_id}">{title}</a> - {escape_html(rating_text)}</li>'
        )
    html.extend(["</ul>", "</body>", "</html>"])
    return "\n".join(html)


def render_recipe_detail_html(recipe):
    html = [
        "<!DOCTYPE html>",
        "<html>",
        f"<head><title>{escape_html(recipe['title'])}</title></head>",
        "<body>",
        f"<h1>{escape_html(recipe['title'])}</h1>",
        f"<p><strong>ID:</strong> {escape_html(recipe['id'])}</p>",
    ]

    if recipe["avgRating"] is None:
        html.append("<p><strong>Average Rating:</strong> No ratings yet</p>")
    else:
        html.append(f"<p><strong>Average Rating:</strong> {recipe['avgRating']:.2f}</p>")

    html.append("<h2>Ingredients</h2><ul>")
    for ingredient in recipe["ingredients"]:
        html.append(f"<li>{escape_html(ingredient)}</li>")
    html.append("</ul>")

    html.append("<h2>Instructions</h2>")
    html.append(f"<p>{escape_html(recipe['instructions'])}</p>")

    html.append("<h2>Comments</h2><ul>")
    if recipe["comments"]:
        for comment in recipe["comments"]:
            html.append(f"<li>{escape_html(comment['comment'])}</li>")
    else:
        html.append("<li>No comments yet</li>")
    html.append("</ul>")

    html.extend(["</body>", "</html>"])
    return "\n".join(html)


def escape_html(value):
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


@app.route("/recipes", methods=["GET"])
def get_recipes_overview():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        r.id,
                        r.title,
                        AVG(rr.rating)::float AS avg_rating,
                        r.created_at
                    FROM recipes r
                    LEFT JOIN recipe_ratings rr ON rr.recipe_id = r.id
                    GROUP BY r.id, r.title, r.created_at
                    ORDER BY
                        avg_rating DESC NULLS LAST,
                        r.created_at DESC
                    LIMIT 50
                    """
                )
                rows = cur.fetchall()

        recipes = [
            {
                "id": row[0],
                "title": row[1],
                "avg_rating": row[2],
                "created_at": row[3],
            }
            for row in rows
        ]
        return Response(render_recipe_overview_html(recipes), mimetype="text/html"), 200
    except Exception:
        return Response("Server error", status=500, mimetype="text/plain")


@app.route("/recipes/upload", methods=["POST"])
def upload_recipe():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid input"}), 400

    title = data.get("title")
    ingredients = data.get("ingredients")
    instructions = data.get("instructions")

    if (
        not isinstance(title, str)
        or not title.strip()
        or not isinstance(instructions, str)
        or not instructions.strip()
        or not isinstance(ingredients, list)
        or len(ingredients) == 0
        or not all(isinstance(item, str) and item.strip() for item in ingredients)
    ):
        return jsonify({"error": "Invalid input"}), 400

    recipe_id = str(uuid.uuid4())

    with get_conn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO recipes (id, title, instructions)
                    VALUES (%s, %s, %s)
                    """,
                    (recipe_id, title.strip(), instructions.strip()),
                )
                for ingredient in ingredients:
                    cur.execute(
                        """
                        INSERT INTO recipe_ingredients (recipe_id, ingredient)
                        VALUES (%s, %s)
                        """,
                        (recipe_id, ingredient.strip()),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    recipe = {
        "id": recipe_id,
        "title": title.strip(),
        "ingredients": [item.strip() for item in ingredients],
        "instructions": instructions.strip(),
        "comments": [],
        "avgRating": None,
    }
    return jsonify(recipe), 201


@app.route("/recipes/<recipe_id>", methods=["GET"])
def get_recipe(recipe_id):
    recipe = fetch_recipe(recipe_id)
    if recipe is None:
        return Response("Recipe not found", status=404, mimetype="text/plain")
    return Response(render_recipe_detail_html(recipe), mimetype="text/html"), 200


@app.route("/recipes/<recipe_id>/comments", methods=["POST"])
def add_comment(recipe_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid input"}), 400

    comment = data.get("comment")
    if not isinstance(comment, str) or not comment.strip():
        return jsonify({"error": "Invalid input"}), 400

    with get_conn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM recipes WHERE id = %s", (recipe_id,))
                if cur.fetchone() is None:
                    conn.rollback()
                    return jsonify({"error": "Recipe not found"}), 404

                cur.execute(
                    """
                    INSERT INTO recipe_comments (recipe_id, comment)
                    VALUES (%s, %s)
                    """,
                    (recipe_id, comment.strip()),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return jsonify({"message": "Comment added successfully"}), 201


@app.route("/recipes/<recipe_id>/ratings", methods=["POST"])
def add_rating(recipe_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid input"}), 400

    rating = data.get("rating")
    if not isinstance(rating, int) or isinstance(rating, bool) or rating < 1 or rating > 5:
        return jsonify({"error": "Invalid input"}), 400

    with get_conn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM recipes WHERE id = %s", (recipe_id,))
                if cur.fetchone() is None:
                    conn.rollback()
                    return jsonify({"error": "Recipe not found"}), 404

                cur.execute(
                    """
                    INSERT INTO recipe_ratings (recipe_id, rating)
                    VALUES (%s, %s)
                    """,
                    (recipe_id, rating),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return jsonify({"message": "Rating added successfully"}), 201


@app.errorhandler(404)
def handle_404(_error):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(405)
def handle_405(_error):
    return jsonify({"error": "Method not allowed"}), 405


@app.errorhandler(500)
def handle_500(_error):
    return jsonify({"error": "Server error"}), 500


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)