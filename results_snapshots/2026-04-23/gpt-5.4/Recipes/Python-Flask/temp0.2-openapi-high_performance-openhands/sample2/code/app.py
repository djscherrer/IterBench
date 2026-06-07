import atexit
import html
import os
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any

import psycopg2
from flask import Flask, Response, abort, jsonify, request
from psycopg2.extensions import STATUS_READY
from psycopg2.pool import ThreadedConnectionPool


DEFAULT_PORT = 5001
SCHEMA_LOCK_ID = 641204851
OVERVIEW_CACHE_TTL_SECONDS = 5.0
OVERVIEW_LIMIT = 10

app = Flask(__name__)

_db_lock = threading.Lock()
_pool: ThreadedConnectionPool | None = None
_pool_pid: int | None = None
_cache_lock = threading.Lock()
_overview_cache: dict[str, Any] = {"html": None, "expires_at": 0.0}


def _db_config() -> dict[str, Any]:
    return {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "user": os.environ.get("DB_USER", "postgres"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "dbname": os.environ.get("DB_NAME", "postgres"),
        "connect_timeout": 5,
        "application_name": f"recipe_sharing_app_{os.getpid()}",
    }


def _pool_size() -> tuple[int, int]:
    cpu_count = os.cpu_count() or 1
    maxconn = int(os.environ.get("DB_POOL_MAX", str(max(4, min(32, cpu_count * 4)))))
    minconn = int(os.environ.get("DB_POOL_MIN", "1"))
    if minconn < 1:
        minconn = 1
    if maxconn < minconn:
        maxconn = minconn
    return minconn, maxconn


def _init_pool_if_needed() -> ThreadedConnectionPool:
    global _pool, _pool_pid

    pid = os.getpid()
    if _pool is not None and _pool_pid == pid:
        return _pool

    with _db_lock:
        if _pool is not None and _pool_pid == pid:
            return _pool

        if _pool is not None:
            try:
                _pool.closeall()
            except Exception:
                pass
            _pool = None
            _pool_pid = None

        minconn, maxconn = _pool_size()
        _pool = ThreadedConnectionPool(minconn=minconn, maxconn=maxconn, **_db_config())
        _pool_pid = pid
        return _pool


@contextmanager
def get_db_connection():
    pool = _init_pool_if_needed()
    conn = pool.getconn()
    try:
        yield conn
    except Exception:
        if not conn.closed and conn.status != STATUS_READY:
            conn.rollback()
        raise
    finally:
        if not conn.closed and conn.status != STATUS_READY:
            conn.rollback()
        pool.putconn(conn)


def close_pool() -> None:
    global _pool, _pool_pid

    with _db_lock:
        if _pool is not None:
            try:
                _pool.closeall()
            finally:
                _pool = None
                _pool_pid = None


atexit.register(close_pool)


def init_db() -> None:
    conn = psycopg2.connect(**_db_config())
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_LOCK_ID,))
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS recipes (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    ingredients TEXT[] NOT NULL,
                    instructions TEXT NOT NULL,
                    rating_sum BIGINT NOT NULL DEFAULT 0,
                    rating_count INTEGER NOT NULL DEFAULT 0,
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
                    rating SMALLINT NOT NULL CHECK (rating >= 1 AND rating <= 5),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes (created_at DESC)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_comments_recipe_id_created_at ON comments (recipe_id, created_at ASC)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id_created_at ON ratings (recipe_id, created_at DESC)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_recipes_rating_sort ON recipes (rating_count DESC, rating_sum DESC, created_at DESC)"
            )
            cur.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK_ID,))
    finally:
        conn.close()


init_db()


def invalidate_overview_cache() -> None:
    with _cache_lock:
        _overview_cache["html"] = None
        _overview_cache["expires_at"] = 0.0


def cached_overview_html() -> str:
    now = time.monotonic()
    with _cache_lock:
        cached_html = _overview_cache["html"]
        if cached_html is not None and _overview_cache["expires_at"] > now:
            return cached_html

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title
                FROM recipes
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (OVERVIEW_LIMIT,),
            )
            recent = cur.fetchall()
            cur.execute(
                """
                SELECT id, title
                FROM recipes
                ORDER BY
                    CASE WHEN rating_count = 0 THEN NULL ELSE rating_sum::DOUBLE PRECISION / rating_count END DESC NULLS LAST,
                    rating_count DESC,
                    created_at DESC
                LIMIT %s
                """,
                (OVERVIEW_LIMIT,),
            )
            top_rated = cur.fetchall()

    rendered = render_overview_html(recent, top_rated)
    with _cache_lock:
        _overview_cache["html"] = rendered
        _overview_cache["expires_at"] = time.monotonic() + OVERVIEW_CACHE_TTL_SECONDS
    return rendered


def recipe_to_api_dict(recipe_row: tuple[Any, ...]) -> dict[str, Any]:
    recipe_id, title, ingredients, instructions, rating_sum, rating_count = recipe_row
    avg_rating = None
    if rating_count:
        avg_rating = round(rating_sum / rating_count, 2)
    return {
        "id": recipe_id,
        "title": title,
        "ingredients": ingredients,
        "instructions": instructions,
        "comments": [],
        "avgRating": avg_rating,
    }


def render_overview_html(recent: list[tuple[str, str]], top_rated: list[tuple[str, str]]) -> str:
    recent_items = "".join(
        f'<li><a href="/recipes/{html.escape(recipe_id)}">{html.escape(title)}</a></li>'
        for recipe_id, title in recent
    ) or "<li>No recipes available.</li>"
    top_items = "".join(
        f'<li><a href="/recipes/{html.escape(recipe_id)}">{html.escape(title)}</a></li>'
        for recipe_id, title in top_rated
    ) or "<li>No rated recipes available.</li>"
    return (
        "<!doctype html>"
        "<html><head><title>Recipe Overview</title></head>"
        "<body>"
        "<h1>Recipe Overview</h1>"
        "<h2>Recent Recipes</h2>"
        f"<ul>{recent_items}</ul>"
        "<h2>Top Rated Recipes</h2>"
        f"<ul>{top_items}</ul>"
        "</body></html>"
    )


def render_recipe_html(recipe: dict[str, Any], comments: list[str]) -> str:
    ingredient_items = "".join(f"<li>{html.escape(item)}</li>" for item in recipe["ingredients"])
    comment_items = "".join(f"<li>{html.escape(comment)}</li>" for comment in comments) or "<li>No comments yet.</li>"
    avg_rating = "Not rated yet" if recipe["avgRating"] is None else str(recipe["avgRating"])
    return (
        "<!doctype html>"
        f"<html><head><title>{html.escape(recipe['title'])}</title></head>"
        "<body>"
        f"<h1>{html.escape(recipe['title'])}</h1>"
        f"<p><strong>Average rating:</strong> {html.escape(avg_rating)}</p>"
        "<h2>Ingredients</h2>"
        f"<ul>{ingredient_items}</ul>"
        "<h2>Instructions</h2>"
        f"<p>{html.escape(recipe['instructions'])}</p>"
        "<h2>Comments</h2>"
        f"<ul>{comment_items}</ul>"
        "</body></html>"
    )


def parse_json_body() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(json_error(400, "Invalid JSON body"))
    return payload


def json_error(status_code: int, message: str) -> Response:
    response = jsonify({"error": message})
    response.status_code = status_code
    return response


def validate_recipe_payload(payload: dict[str, Any]) -> tuple[str, list[str], str] | None:
    title = payload.get("title")
    ingredients = payload.get("ingredients")
    instructions = payload.get("instructions")

    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(instructions, str) or not instructions.strip():
        return None
    if not isinstance(ingredients, list) or not ingredients:
        return None

    cleaned_ingredients: list[str] = []
    for ingredient in ingredients:
        if not isinstance(ingredient, str) or not ingredient.strip():
            return None
        cleaned_ingredients.append(ingredient.strip())

    return title.strip(), cleaned_ingredients, instructions.strip()


@app.get("/recipes")
def get_recipes() -> Response:
    return Response(cached_overview_html(), mimetype="text/html")


@app.post("/recipes/upload")
def upload_recipe() -> Response:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return json_error(400, "Invalid JSON body")

    validated = validate_recipe_payload(payload)
    if validated is None:
        return json_error(400, "Invalid recipe payload")

    title, ingredients, instructions = validated
    recipe_id = uuid.uuid4().hex

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO recipes (id, title, ingredients, instructions)
                VALUES (%s, %s, %s, %s)
                RETURNING id, title, ingredients, instructions, rating_sum, rating_count
                """,
                (recipe_id, title, ingredients, instructions),
            )
            created = cur.fetchone()
        conn.commit()

    invalidate_overview_cache()
    response = jsonify(recipe_to_api_dict(created))
    response.status_code = 201
    return response


@app.get("/recipes/<string:recipe_id>")
def get_recipe(recipe_id: str) -> Response:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, ingredients, instructions, rating_sum, rating_count
                FROM recipes
                WHERE id = %s
                """,
                (recipe_id,),
            )
            recipe_row = cur.fetchone()
            if recipe_row is None:
                abort(404)
            cur.execute(
                """
                SELECT comment
                FROM comments
                WHERE recipe_id = %s
                ORDER BY created_at ASC, id ASC
                """,
                (recipe_id,),
            )
            comments = [row[0] for row in cur.fetchall()]

    recipe = recipe_to_api_dict(recipe_row)
    recipe["comments"] = [{"comment": comment} for comment in comments]
    return Response(render_recipe_html(recipe, comments), mimetype="text/html")


@app.post("/recipes/<string:recipe_id>/comments")
def add_comment(recipe_id: str) -> Response:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return json_error(400, "Invalid JSON body")

    comment = payload.get("comment")
    if not isinstance(comment, str) or not comment.strip():
        return json_error(400, "Invalid comment payload")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO comments (recipe_id, comment)
                SELECT id, %s
                FROM recipes
                WHERE id = %s
                RETURNING id
                """,
                (comment.strip(), recipe_id),
            )
            created = cur.fetchone()
        conn.commit()

    if created is None:
        return json_error(404, "Recipe not found")

    return jsonify({"status": "created"}), 201


@app.post("/recipes/<string:recipe_id>/ratings")
def add_rating(recipe_id: str) -> Response:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return json_error(400, "Invalid JSON body")

    rating = payload.get("rating")
    if isinstance(rating, bool) or not isinstance(rating, int) or rating < 1 or rating > 5:
        return json_error(400, "Invalid rating payload")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH updated AS (
                    UPDATE recipes
                    SET rating_sum = rating_sum + %s,
                        rating_count = rating_count + 1
                    WHERE id = %s
                    RETURNING id
                )
                INSERT INTO ratings (recipe_id, rating)
                SELECT id, %s
                FROM updated
                RETURNING id
                """,
                (rating, recipe_id, rating),
            )
            created = cur.fetchone()
        conn.commit()

    if created is None:
        return json_error(404, "Recipe not found")

    invalidate_overview_cache()
    return jsonify({"status": "created"}), 201


@app.errorhandler(404)
def not_found(_: Exception) -> Response:
    return json_error(404, "Recipe not found") if request.path.startswith("/recipes/") and request.method != "GET" else Response("Recipe not found", status=404, mimetype="text/plain")


@app.errorhandler(500)
def internal_error(_: Exception) -> Response:
    if request.path.startswith("/recipes") and request.method != "GET":
        return json_error(500, "Server error")
    return Response("Server error", status=500, mimetype="text/plain")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", str(DEFAULT_PORT))))
