import os
import uuid
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, jsonify, request, Response, abort, url_for
from markupsafe import escape

app = Flask(__name__)


def get_db_config():
    return {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "user": os.environ.get("DB_USER", "postgres"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "dbname": os.environ.get("DB_NAME", "postgres"),
    }


@contextmanager
def get_db_connection():
    conn = psycopg2.connect(**get_db_config())
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    statements = [
        """
        CREATE TABLE IF NOT EXISTS recipes (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            instructions TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS recipe_ingredients (
            id BIGSERIAL PRIMARY KEY,
            recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            ingredient TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS recipe_comments (
            id BIGSERIAL PRIMARY KEY,
            recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            comment TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS recipe_ratings (
            id BIGSERIAL PRIMARY KEY,
            recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
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
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
        conn.commit()


def recipe_exists(conn, recipe_id):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM recipes WHERE id = %s", (recipe_id,))
        return cur.fetchone() is not None


def fetch_recipe(conn, recipe_id):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                r.id,
                r.title,
                r.instructions,
                COALESCE(
                    (
                        SELECT json_agg(ri.ingredient ORDER BY ri.id)
                        FROM recipe_ingredients ri
                        WHERE ri.recipe_id = r.id
                    ),
                    '[]'::json
                ) AS ingredients,
                COALESCE(
                    (
                        SELECT json_agg(json_build_object('comment', rc.comment) ORDER BY rc.id)
                        FROM recipe_comments rc
                        WHERE rc.recipe_id = r.id
                    ),
                    '[]'::json
                ) AS comments,
                (
                    SELECT ROUND(AVG(rr.rating)::numeric, 2)
                    FROM recipe_ratings rr
                    WHERE rr.recipe_id = r.id
                ) AS "avgRating"
            FROM recipes r
            WHERE r.id = %s
            """,
            (recipe_id,),
        )
        return cur.fetchone()


@app.errorhandler(400)
def handle_400(error):
    return jsonify({"error": "Invalid input"}), 400


@app.errorhandler(404)
def handle_404(error):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def handle_500(error):
    return jsonify({"error": "Server error"}), 500


@app.route("/recipes", methods=["GET"])
def get_recipes_overview():
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT
                        r.id,
                        r.title,
                        r.created_at,
                        (
                            SELECT AVG(rr.rating)
                            FROM recipe_ratings rr
                            WHERE rr.recipe_id = r.id
                        ) AS avg_rating
                    FROM recipes r
                    ORDER BY r.created_at DESC
                    LIMIT 20
                    """
                )
                recent = cur.fetchall()

                cur.execute(
                    """
                    SELECT
                        r.id,
                        r.title,
                        (
                            SELECT AVG(rr.rating)
                            FROM recipe_ratings rr
                            WHERE rr.recipe_id = r.id
                        ) AS avg_rating
                    FROM recipes r
                    WHERE EXISTS (
                        SELECT 1 FROM recipe_ratings rr WHERE rr.recipe_id = r.id
                    )
                    ORDER BY avg_rating DESC NULLS LAST, r.created_at DESC
                    LIMIT 20
                    """
                )
                top_rated = cur.fetchall()

        def render_recipe_list(items):
            if not items:
                return "<p>No recipes found.</p>"
            parts = ["<ul>"]
            for item in items:
                rid = escape(item["id"])
                title = escape(item["title"])
                avg = item.get("avg_rating")
                avg_text = f" - Avg rating: {float(avg):.2f}" if avg is not None else ""
                link = url_for("get_recipe", recipeId=item["id"])
                parts.append(f'<li><a href="{escape(link)}">{title}</a>{escape(avg_text)}</li>')
            parts.append("</ul>")
            return "".join(parts)

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Recipe Overview</title>
        </head>
        <body>
            <h1>Recipe Overview</h1>
            <h2>Recent Recipes</h2>
            {render_recipe_list(recent)}
            <h2>Top Rated Recipes</h2>
            {render_recipe_list(top_rated)}
        </body>
        </html>
        """
        return Response(html, mimetype="text/html"), 200
    except Exception:
        return Response("Server error", status=500, mimetype="text/plain")


@app.route("/recipes/upload", methods=["POST"])
def upload_recipe():
    if not request.is_json:
        return jsonify({"error": "Invalid input"}), 400

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
        or any(not isinstance(i, str) or not i.strip() for i in ingredients)
    ):
        return jsonify({"error": "Invalid input"}), 400

    recipe_id = str(uuid.uuid4())

    with get_db_connection() as conn:
        conn.autocommit = False
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

        recipe = fetch_recipe(conn, recipe_id)

    return jsonify(recipe), 201


@app.route("/recipes/<recipeId>", methods=["GET"])
def get_recipe(recipeId):
    with get_db_connection() as conn:
        recipe = fetch_recipe(conn, recipeId)

    if not recipe:
        return Response("Recipe not found", status=404, mimetype="text/plain")

    ingredients_html = "".join(f"<li>{escape(item)}</li>" for item in recipe["ingredients"])
    comments = recipe["comments"] or []
    comments_html = (
        "".join(f"<li>{escape(item.get('comment', ''))}</li>" for item in comments)
        if comments
        else "<li>No comments yet.</li>"
    )

    avg_rating = recipe["avgRating"]
    avg_rating_html = "No ratings yet." if avg_rating is None else f"{float(avg_rating):.2f}/5"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{escape(recipe["title"])}</title>
    </head>
    <body>
        <h1>{escape(recipe["title"])}</h1>
        <h2>Ingredients</h2>
        <ul>
            {ingredients_html}
        </ul>
        <h2>Instructions</h2>
        <p>{escape(recipe["instructions"])}</p>
        <h2>Average Rating</h2>
        <p>{escape(avg_rating_html)}</p>
        <h2>Comments</h2>
        <ul>
            {comments_html}
        </ul>
    </body>
    </html>
    """
    return Response(html, mimetype="text/html"), 200


@app.route("/recipes/<recipeId>/comments", methods=["POST"])
def add_comment(recipeId):
    if not request.is_json:
        return jsonify({"error": "Invalid input"}), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid input"}), 400

    comment = data.get("comment")
    if not isinstance(comment, str) or not comment.strip():
        return jsonify({"error": "Invalid input"}), 400

    with get_db_connection() as conn:
        conn.autocommit = False
        try:
            if not recipe_exists(conn, recipeId):
                return jsonify({"error": "Recipe not found"}), 404

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO recipe_comments (recipe_id, comment)
                    VALUES (%s, %s)
                    """,
                    (recipeId, comment.strip()),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return jsonify({"message": "Comment added successfully"}), 201


@app.route("/recipes/<recipeId>/ratings", methods=["POST"])
def add_rating(recipeId):
    if not request.is_json:
        return jsonify({"error": "Invalid input"}), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid input"}), 400

    rating = data.get("rating")
    if not isinstance(rating, int) or isinstance(rating, bool) or rating < 1 or rating > 5:
        return jsonify({"error": "Invalid input"}), 400

    with get_db_connection() as conn:
        conn.autocommit = False
        try:
            if not recipe_exists(conn, recipeId):
                return jsonify({"error": "Recipe not found"}), 404

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO recipe_ratings (recipe_id, rating)
                    VALUES (%s, %s)
                    """,
                    (recipeId, rating),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return jsonify({"message": "Rating added successfully"}), 201


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port)