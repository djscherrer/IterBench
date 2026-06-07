import os
import uuid
from contextlib import contextmanager

import psycopg2
from psycopg2 import pool
from flask import Flask, jsonify, request, Response, abort

app = Flask(__name__)

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "postgres")
PORT = int(os.environ.get("PORT", "5001"))

db_pool = pool.SimpleConnectionPool(
    minconn=1,
    maxconn=max(4, (os.cpu_count() or 1) * 2),
    host=DB_HOST,
    port=DB_PORT,
    user=DB_USER,
    password=DB_PASSWORD,
    dbname=DB_NAME,
)


@contextmanager
def get_db_connection():
    conn = db_pool.getconn()
    try:
        yield conn
    finally:
        db_pool.putconn(conn)


def init_db():
    statements = [
        """
        CREATE TABLE IF NOT EXISTS recipes (
            id UUID PRIMARY KEY,
            title TEXT NOT NULL,
            instructions TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS recipe_ingredients (
            id BIGSERIAL PRIMARY KEY,
            recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            ingredient TEXT NOT NULL,
            position INTEGER NOT NULL,
            UNIQUE(recipe_id, position)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS recipe_comments (
            id BIGSERIAL PRIMARY KEY,
            recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            comment TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS recipe_ratings (
            id BIGSERIAL PRIMARY KEY,
            recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_recipe_id
        ON recipe_ingredients(recipe_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_recipe_comments_recipe_id
        ON recipe_comments(recipe_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_recipe_ratings_recipe_id
        ON recipe_ratings(recipe_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_recipes_created_at
        ON recipes(created_at DESC)
        """,
    ]

    with get_db_connection() as conn:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                for stmt in statements:
                    cur.execute(stmt)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


init_db()


def validate_recipe_payload(data):
    if not isinstance(data, dict):
        return "Request body must be a JSON object."

    title = data.get("title")
    ingredients = data.get("ingredients")
    instructions = data.get("instructions")

    if not isinstance(title, str) or not title.strip():
        return "Field 'title' is required and must be a non-empty string."

    if not isinstance(ingredients, list) or not ingredients:
        return "Field 'ingredients' is required and must be a non-empty array of strings."

    for ingredient in ingredients:
        if not isinstance(ingredient, str) or not ingredient.strip():
            return "Each ingredient must be a non-empty string."

    if not isinstance(instructions, str) or not instructions.strip():
        return "Field 'instructions' is required and must be a non-empty string."

    return None


def recipe_exists(conn, recipe_id):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM recipes WHERE id = %s", (recipe_id,))
        return cur.fetchone() is not None


def fetch_recipe_data(conn, recipe_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                r.id::text,
                r.title,
                r.instructions,
                COALESCE(AVG(rr.rating)::numeric(10,2), NULL) AS avg_rating
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
            ORDER BY position ASC
            """,
            (recipe_id,),
        )
        ingredients = [row[0] for row in cur.fetchall()]

        cur.execute(
            """
            SELECT comment
            FROM recipe_comments
            WHERE recipe_id = %s
            ORDER BY created_at ASC, id ASC
            """,
            (recipe_id,),
        )
        comments = [{"comment": row[0]} for row in cur.fetchall()]

        avg_rating = float(recipe_row[3]) if recipe_row[3] is not None else None

        return {
            "id": recipe_row[0],
            "title": recipe_row[1],
            "ingredients": ingredients,
            "instructions": recipe_row[2],
            "comments": comments,
            "avgRating": avg_rating,
        }


def render_recipe_overview_html(recipes):
    items = []
    for recipe in recipes:
        avg_text = "No ratings yet" if recipe["avg_rating"] is None else f'Avg rating: {recipe["avg_rating"]:.2f}'
        items.append(
            f"""
            <li>
                <a href="/recipes/{recipe["id"]}">{escape_html(recipe["title"])}</a>
                - {escape_html(avg_text)}
            </li>
            """
        )

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>Recipe Overview</title>
    </head>
    <body>
        <h1>Recipe Overview</h1>
        <ul>
            {''.join(items) if items else '<li>No recipes available.</li>'}
        </ul>
    </body>
    </html>
    """


def render_recipe_html(recipe):
    ingredients_html = "".join(
        f"<li>{escape_html(ingredient)}</li>" for ingredient in recipe["ingredients"]
    ) or "<li>No ingredients listed.</li>"

    comments_html = "".join(
        f"<li>{escape_html(comment['comment'])}</li>" for comment in recipe["comments"]
    ) or "<li>No comments yet.</li>"

    avg_text = "No ratings yet" if recipe["avgRating"] is None else f'{recipe["avgRating"]:.2f} / 5'

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>{escape_html(recipe["title"])}</title>
    </head>
    <body>
        <h1>{escape_html(recipe["title"])}</h1>
        <p><strong>ID:</strong> {escape_html(recipe["id"])}</p>
        <p><strong>Average Rating:</strong> {escape_html(avg_text)}</p>

        <h2>Ingredients</h2>
        <ul>
            {ingredients_html}
        </ul>

        <h2>Instructions</h2>
        <p>{escape_html(recipe["instructions"])}</p>

        <h2>Comments</h2>
        <ul>
            {comments_html}
        </ul>
    </body>
    </html>
    """


def escape_html(value):
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


@app.errorhandler(400)
def bad_request(error):
    description = getattr(error, "description", "Invalid input")
    return jsonify({"error": description}), 400


@app.errorhandler(404)
def not_found(error):
    description = getattr(error, "description", "Resource not found")
    return jsonify({"error": description}), 404


@app.errorhandler(500)
def internal_server_error(error):
    return jsonify({"error": "Server error"}), 500


@app.get("/recipes")
def get_recipes_overview():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    r.id::text,
                    r.title,
                    AVG(rr.rating)::numeric(10,2) AS avg_rating,
                    r.created_at
                FROM recipes r
                LEFT JOIN recipe_ratings rr ON rr.recipe_id = r.id
                GROUP BY r.id, r.title, r.created_at
                ORDER BY
                    COALESCE(AVG(rr.rating), 0) DESC,
                    r.created_at DESC
                LIMIT 50
                """
            )
            rows = cur.fetchall()

    recipes = [
        {
            "id": row[0],
            "title": row[1],
            "avg_rating": float(row[2]) if row[2] is not None else None,
            "created_at": row[3],
        }
        for row in rows
    ]

    html = render_recipe_overview_html(recipes)
    return Response(html, status=200, mimetype="text/html")


@app.post("/recipes/upload")
def upload_recipe():
    data = request.get_json(silent=True)
    error = validate_recipe_payload(data)
    if error:
        abort(400, description=error)

    recipe_id = str(uuid.uuid4())
    title = data["title"].strip()
    ingredients = [ingredient.strip() for ingredient in data["ingredients"]]
    instructions = data["instructions"].strip()

    with get_db_connection() as conn:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO recipes (id, title, instructions)
                    VALUES (%s, %s, %s)
                    """,
                    (recipe_id, title, instructions),
                )

                for index, ingredient in enumerate(ingredients):
                    cur.execute(
                        """
                        INSERT INTO recipe_ingredients (recipe_id, ingredient, position)
                        VALUES (%s, %s, %s)
                        """,
                        (recipe_id, ingredient, index),
                    )

            conn.commit()
        except Exception:
            conn.rollback()
            raise

    response_body = {
        "id": recipe_id,
        "title": title,
        "ingredients": ingredients,
        "instructions": instructions,
        "comments": [],
        "avgRating": None,
    }
    return jsonify(response_body), 201


@app.get("/recipes/<recipe_id>")
def get_recipe(recipe_id):
    with get_db_connection() as conn:
        recipe = fetch_recipe_data(conn, recipe_id)

    if recipe is None:
        abort(404, description="Recipe not found")

    html = render_recipe_html(recipe)
    return Response(html, status=200, mimetype="text/html")


@app.post("/recipes/<recipe_id>/comments")
def add_comment(recipe_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, description="Request body must be a JSON object.")

    comment = data.get("comment")
    if not isinstance(comment, str) or not comment.strip():
        abort(400, description="Field 'comment' is required and must be a non-empty string.")

    with get_db_connection() as conn:
        conn.autocommit = False
        try:
            if not recipe_exists(conn, recipe_id):
                abort(404, description="Recipe not found")

            with conn.cursor() as cur:
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


@app.post("/recipes/<recipe_id>/ratings")
def add_rating(recipe_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, description="Request body must be a JSON object.")

    rating = data.get("rating")
    if not isinstance(rating, int) or isinstance(rating, bool) or rating < 1 or rating > 5:
        abort(400, description="Field 'rating' is required and must be an integer between 1 and 5.")

    with get_db_connection() as conn:
        conn.autocommit = False
        try:
            if not recipe_exists(conn, recipe_id):
                abort(404, description="Recipe not found")

            with conn.cursor() as cur:
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)