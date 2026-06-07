import os
import uuid
import html
import threading
from contextlib import contextmanager
from time import monotonic
from urllib.parse import quote

from flask import Flask, Response, jsonify, request
import psycopg2
from psycopg2 import pool
from psycopg2.extras import Json


app = Flask(__name__)

SCHEMA_LOCK_ID = 842691377206441
DEFAULT_CACHE_TTL_SECONDS = 2.0
DETAIL_CACHE_TTL_SECONDS = 5.0

_db_pool = None
_db_pool_pid = None
_db_pool_lock = threading.Lock()

_overview_cache = {"expires_at": 0.0, "html": None}
_overview_cache_lock = threading.Lock()
_recipe_cache = {}
_recipe_cache_lock = threading.Lock()


def _db_config():
    return {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "user": os.environ.get("DB_USER", "postgres"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "dbname": os.environ.get("DB_NAME", "postgres"),
        "connect_timeout": 5,
        "application_name": "recipe_sharing_api",
    }


def _new_connection():
    return psycopg2.connect(**_db_config())


def init_db():
    conn = _new_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS recipes (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        ingredients JSONB NOT NULL,
                        instructions TEXT NOT NULL,
                        rating_sum BIGINT NOT NULL DEFAULT 0,
                        rating_count BIGINT NOT NULL DEFAULT 0,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS comments (
                        id BIGSERIAL PRIMARY KEY,
                        recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                        comment TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ratings (
                        id BIGSERIAL PRIMARY KEY,
                        recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                        rating SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes (created_at DESC, id)"
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_recipes_avg_rating
                    ON recipes (
                        (CASE WHEN rating_count > 0
                              THEN (rating_sum::double precision / rating_count)
                              ELSE NULL END) DESC NULLS LAST,
                        rating_count DESC,
                        created_at DESC
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_comments_recipe_created ON comments (recipe_id, created_at ASC, id ASC)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ratings_recipe_created ON ratings (recipe_id, created_at DESC, id DESC)"
                )
    finally:
        conn.close()


def get_pool():
    global _db_pool, _db_pool_pid
    current_pid = os.getpid()
    if _db_pool is not None and _db_pool_pid == current_pid:
        return _db_pool

    with _db_pool_lock:
        if _db_pool is not None and _db_pool_pid == current_pid:
            return _db_pool

        if _db_pool is not None and _db_pool_pid != current_pid:
            try:
                _db_pool.closeall()
            except Exception:
                pass

        max_connections = max(1, int(os.environ.get("DB_POOL_SIZE", "8")))
        _db_pool = pool.ThreadedConnectionPool(1, max_connections, **_db_config())
        _db_pool_pid = current_pid
        return _db_pool


@contextmanager
def db_cursor():
    db_pool = get_pool()
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        db_pool.putconn(conn)


def invalidate_overview_cache():
    with _overview_cache_lock:
        _overview_cache["expires_at"] = 0.0
        _overview_cache["html"] = None


def invalidate_recipe_cache(recipe_id):
    with _recipe_cache_lock:
        _recipe_cache.pop(recipe_id, None)


def error_response(message, status):
    return jsonify({"error": message}), status


def clean_nonempty_string(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def validate_recipe_payload(data):
    if not isinstance(data, dict):
        return None, "Request body must be a JSON object"

    title = clean_nonempty_string(data.get("title"))
    instructions = clean_nonempty_string(data.get("instructions"))
    ingredients = data.get("ingredients")

    if title is None:
        return None, "title is required and must be a non-empty string"
    if instructions is None:
        return None, "instructions is required and must be a non-empty string"
    if not isinstance(ingredients, list) or not ingredients:
        return None, "ingredients is required and must be a non-empty array of strings"

    cleaned_ingredients = []
    for ingredient in ingredients:
        cleaned = clean_nonempty_string(ingredient)
        if cleaned is None:
            return None, "ingredients must contain only non-empty strings"
        cleaned_ingredients.append(cleaned)

    return {
        "title": title,
        "ingredients": cleaned_ingredients,
        "instructions": instructions,
    }, None


def validate_comment_payload(data):
    if not isinstance(data, dict):
        return None, "Request body must be a JSON object"
    comment = clean_nonempty_string(data.get("comment"))
    if comment is None:
        return None, "comment is required and must be a non-empty string"
    return comment, None


def validate_rating_payload(data):
    if not isinstance(data, dict):
        return None, "Request body must be a JSON object"
    rating = data.get("rating")
    if isinstance(rating, bool) or not isinstance(rating, int) or rating < 1 or rating > 5:
        return None, "rating is required and must be an integer between 1 and 5"
    return rating, None


def recipe_json(recipe_id, title, ingredients, instructions, rating_sum=0, rating_count=0, comments=None):
    avg_rating = None
    if rating_count:
        avg_rating = float(rating_sum) / float(rating_count)
    return {
        "id": recipe_id,
        "title": title,
        "ingredients": ingredients,
        "instructions": instructions,
        "comments": comments if comments is not None else [],
        "avgRating": avg_rating,
    }


def html_link(recipe_id, title):
    return '<a href="/recipes/{0}">{1}</a>'.format(
        quote(str(recipe_id), safe=""), html.escape(title, quote=True)
    )


def render_overview(recent, top_rated):
    parts = [
        "<!doctype html>",
        "<html><head><meta charset=\"utf-8\"><title>Recipes</title></head><body>",
        "<h1>Recipes</h1>",
        "<h2>Recent Recipes</h2><ul>",
    ]
    if recent:
        for recipe_id, title in recent:
            parts.append(f"<li>{html_link(recipe_id, title)}</li>")
    else:
        parts.append("<li>No recipes uploaded yet.</li>")

    parts.append("</ul><h2>Top-rated Recipes</h2><ul>")
    if top_rated:
        for recipe_id, title, avg_rating in top_rated:
            rating_text = "" if avg_rating is None else " - {:.2f}/5".format(float(avg_rating))
            parts.append(f"<li>{html_link(recipe_id, title)}{rating_text}</li>")
    else:
        parts.append("<li>No rated recipes yet.</li>")

    parts.append("</ul></body></html>")
    return "".join(parts)


def render_recipe_detail(recipe, comments):
    recipe_id, title, ingredients, instructions, rating_sum, rating_count = recipe
    avg_rating = None
    if rating_count:
        avg_rating = float(rating_sum) / float(rating_count)

    parts = [
        "<!doctype html>",
        "<html><head><meta charset=\"utf-8\"><title>",
        html.escape(title, quote=True),
        "</title></head><body>",
        "<p><a href=\"/recipes\">Back to recipes</a></p>",
        "<h1>",
        html.escape(title, quote=True),
        "</h1>",
        "<h2>Ingredients</h2><ul>",
    ]
    for ingredient in ingredients:
        parts.append("<li>{}</li>".format(html.escape(str(ingredient), quote=True)))
    parts.extend([
        "</ul><h2>Instructions</h2><p>",
        html.escape(instructions, quote=True).replace("\n", "<br>"),
        "</p><h2>Rating</h2><p>",
        "No ratings yet" if avg_rating is None else "{:.2f}/5 ({} ratings)".format(avg_rating, rating_count),
        "</p><h2>Comments</h2><ul>",
    ])
    if comments:
        for (comment,) in comments:
            parts.append("<li>{}</li>".format(html.escape(comment, quote=True)))
    else:
        parts.append("<li>No comments yet.</li>")
    parts.append("</ul></body></html>")
    return "".join(parts)


@app.get("/recipes")
def get_recipes():
    now = monotonic()
    cached = _overview_cache["html"]
    if cached is not None and _overview_cache["expires_at"] > now:
        return Response(cached, mimetype="text/html")

    with db_cursor() as cur:
        cur.execute(
            "SELECT id, title FROM recipes ORDER BY created_at DESC, id DESC LIMIT 20"
        )
        recent = cur.fetchall()
        cur.execute(
            """
            SELECT id, title,
                   (rating_sum::double precision / NULLIF(rating_count, 0)) AS avg_rating
            FROM recipes
            WHERE rating_count > 0
            ORDER BY avg_rating DESC, rating_count DESC, created_at DESC
            LIMIT 20
            """
        )
        top_rated = cur.fetchall()

    page = render_overview(recent, top_rated)
    with _overview_cache_lock:
        _overview_cache["html"] = page
        _overview_cache["expires_at"] = monotonic() + DEFAULT_CACHE_TTL_SECONDS
    return Response(page, mimetype="text/html")


@app.post("/recipes/upload")
def upload_recipe():
    payload, error = validate_recipe_payload(request.get_json(silent=True))
    if error:
        return error_response(error, 400)

    recipe_id = str(uuid.uuid4())
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO recipes (id, title, ingredients, instructions)
            VALUES (%s, %s, %s, %s)
            """,
            (recipe_id, payload["title"], Json(payload["ingredients"]), payload["instructions"]),
        )

    invalidate_overview_cache()
    return jsonify(recipe_json(
        recipe_id,
        payload["title"],
        payload["ingredients"],
        payload["instructions"],
    )), 201


@app.get("/recipes/<recipe_id>")
def get_recipe(recipe_id):
    now = monotonic()
    cached = _recipe_cache.get(recipe_id)
    if cached is not None and cached[0] > now:
        return Response(cached[1], mimetype="text/html")

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id, title, ingredients, instructions, rating_sum, rating_count
            FROM recipes
            WHERE id = %s
            """,
            (recipe_id,),
        )
        recipe = cur.fetchone()
        if recipe is None:
            return error_response("Recipe not found", 404)

        cur.execute(
            "SELECT comment FROM comments WHERE recipe_id = %s ORDER BY created_at ASC, id ASC",
            (recipe_id,),
        )
        comments = cur.fetchall()

    page = render_recipe_detail(recipe, comments)
    with _recipe_cache_lock:
        _recipe_cache[recipe_id] = (monotonic() + DETAIL_CACHE_TTL_SECONDS, page)
    return Response(page, mimetype="text/html")


@app.post("/recipes/<recipe_id>/comments")
def add_comment(recipe_id):
    comment, error = validate_comment_payload(request.get_json(silent=True))
    if error:
        return error_response(error, 400)

    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO comments (recipe_id, comment)
            SELECT id, %s FROM recipes WHERE id = %s
            RETURNING id
            """,
            (comment, recipe_id),
        )
        inserted = cur.fetchone()

    if inserted is None:
        return error_response("Recipe not found", 404)

    invalidate_recipe_cache(recipe_id)
    return jsonify({"message": "Comment added successfully"}), 201


@app.post("/recipes/<recipe_id>/ratings")
def add_rating(recipe_id):
    rating, error = validate_rating_payload(request.get_json(silent=True))
    if error:
        return error_response(error, 400)

    with db_cursor() as cur:
        cur.execute(
            """
            WITH inserted AS (
                INSERT INTO ratings (recipe_id, rating)
                SELECT id, %s FROM recipes WHERE id = %s
                RETURNING recipe_id
            )
            UPDATE recipes
            SET rating_sum = rating_sum + %s,
                rating_count = rating_count + 1
            WHERE id = %s AND EXISTS (SELECT 1 FROM inserted)
            RETURNING id
            """,
            (rating, recipe_id, rating, recipe_id),
        )
        updated = cur.fetchone()

    if updated is None:
        return error_response("Recipe not found", 404)

    invalidate_recipe_cache(recipe_id)
    invalidate_overview_cache()
    return jsonify({"message": "Rating added successfully"}), 201


@app.errorhandler(404)
def not_found(_error):
    return error_response("Not found", 404)


@app.errorhandler(500)
def internal_error(_error):
    return error_response("Server error", 500)


init_db()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
