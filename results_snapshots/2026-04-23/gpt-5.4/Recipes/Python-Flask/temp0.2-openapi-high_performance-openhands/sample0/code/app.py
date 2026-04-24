from __future__ import annotations

import os
import threading
import uuid
from contextlib import contextmanager
from html import escape
from typing import Any
from urllib.parse import quote

import psycopg2
from flask import Flask, Response, jsonify, request
from psycopg2.extras import Json
from psycopg2.pool import ThreadedConnectionPool

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

SCHEMA_LOCK_ID = 824611239571
OVERVIEW_LIMIT = 20


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


DB_SETTINGS = {
    "host": _required_env("DB_HOST"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "user": _required_env("DB_USER"),
    "password": _required_env("DB_PASSWORD"),
    "dbname": _required_env("DB_NAME"),
    "connect_timeout": 5,
}

POOL_MIN = max(1, int(os.getenv("DB_POOL_MIN", "1")))
POOL_MAX = max(POOL_MIN, int(os.getenv("DB_POOL_MAX", "16")))

_pool_lock = threading.Lock()
_db_init_lock = threading.Lock()
_db_pool: ThreadedConnectionPool | None = None
_db_pool_pid: int | None = None
_db_initialized = False


def init_database() -> None:
    conn = psycopg2.connect(**DB_SETTINGS)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_LOCK_ID,))
            try:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS recipes (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL CHECK (char_length(title) > 0),
                        ingredients JSONB NOT NULL CHECK (jsonb_typeof(ingredients) = 'array'),
                        instructions TEXT NOT NULL CHECK (char_length(instructions) > 0),
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        rating_sum BIGINT NOT NULL DEFAULT 0,
                        rating_count INTEGER NOT NULL DEFAULT 0,
                        avg_rating DOUBLE PRECISION,
                        comment_count INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS comments (
                        id BIGSERIAL PRIMARY KEY,
                        recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                        comment TEXT NOT NULL CHECK (char_length(comment) > 0),
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
                    "CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes (created_at DESC)"
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_recipes_top_rated
                    ON recipes (avg_rating DESC NULLS LAST, rating_count DESC, created_at DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_comments_recipe_created_at
                    ON comments (recipe_id, created_at ASC, id ASC)
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id ON ratings (recipe_id)"
                )
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK_ID,))
    finally:
        conn.close()


def ensure_database_ready() -> None:
    global _db_initialized
    if _db_initialized:
        return
    with _db_init_lock:
        if _db_initialized:
            return
        init_database()
        _db_initialized = True


def get_pool() -> ThreadedConnectionPool:
    global _db_pool, _db_pool_pid
    ensure_database_ready()
    pid = os.getpid()
    if _db_pool is None or _db_pool_pid != pid:
        with _pool_lock:
            if _db_pool is None or _db_pool_pid != pid:
                if _db_pool is not None:
                    try:
                        _db_pool.closeall()
                    except Exception:
                        pass
                _db_pool = ThreadedConnectionPool(POOL_MIN, POOL_MAX, **DB_SETTINGS)
                _db_pool_pid = pid
    return _db_pool


@contextmanager
def db_connection():
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)


def json_error(message: str, status: int):
    return jsonify({"error": message}), status


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def validate_recipe_payload(data: Any) -> tuple[str, list[str], str] | None:
    if not isinstance(data, dict):
        return None
    title = _clean_text(data.get("title"))
    instructions = _clean_text(data.get("instructions"))
    ingredients_raw = data.get("ingredients")
    if title is None or instructions is None or not isinstance(ingredients_raw, list):
        return None
    ingredients: list[str] = []
    for item in ingredients_raw:
        cleaned = _clean_text(item)
        if cleaned is None:
            return None
        ingredients.append(cleaned)
    return title, ingredients, instructions


def validate_comment_payload(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    return _clean_text(data.get("comment"))


def validate_rating_payload(data: Any) -> int | None:
    if not isinstance(data, dict):
        return None
    rating = data.get("rating")
    if type(rating) is not int or not 1 <= rating <= 5:
        return None
    return rating


def render_overview_page(recent_rows: list[tuple[str, str]], top_rows: list[tuple[str, str]]) -> str:
    recent_items = "".join(
        f'<li><a href="/recipes/{quote(recipe_id)}">{escape(title)}</a></li>'
        for recipe_id, title in recent_rows
    ) or "<li>No recipes available.</li>"
    top_items = "".join(
        f'<li><a href="/recipes/{quote(recipe_id)}">{escape(title)}</a></li>'
        for recipe_id, title in top_rows
    ) or "<li>No ratings yet.</li>"
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>Recipe Overview</title></head><body>"
        "<h1>Recipe Overview</h1>"
        "<h2>Recent Recipes</h2><ul>"
        f"{recent_items}"
        "</ul><h2>Top Rated Recipes</h2><ul>"
        f"{top_items}"
        "</ul></body></html>"
    )


def render_recipe_page(
    recipe_id: str,
    title: str,
    ingredients: list[Any],
    instructions: str,
    avg_rating: float | None,
    rating_count: int,
    comments: list[tuple[str]],
) -> str:
    ingredient_items = "".join(f"<li>{escape(str(item))}</li>" for item in ingredients) or "<li>None</li>"
    comment_items = "".join(f"<li>{escape(comment)}</li>" for comment, in comments) or "<li>No comments yet.</li>"
    rating_text = "Not rated yet" if avg_rating is None else f"{avg_rating:.2f} ({rating_count} ratings)"
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{escape(title)}</title></head><body>"
        f"<h1>{escape(title)}</h1>"
        f"<p><strong>ID:</strong> {escape(recipe_id)}</p>"
        f"<p><strong>Average rating:</strong> {escape(rating_text)}</p>"
        "<h2>Ingredients</h2><ul>"
        f"{ingredient_items}"
        "</ul><h2>Instructions</h2>"
        f"<pre>{escape(instructions)}</pre>"
        "<h2>Comments</h2><ul>"
        f"{comment_items}"
        "</ul></body></html>"
    )


@app.get("/recipes")
def list_recipes() -> Response:
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title FROM recipes ORDER BY created_at DESC LIMIT %s",
                (OVERVIEW_LIMIT,),
            )
            recent_rows = cur.fetchall()
            cur.execute(
                """
                SELECT id, title
                FROM recipes
                WHERE avg_rating IS NOT NULL
                ORDER BY avg_rating DESC, rating_count DESC, created_at DESC
                LIMIT %s
                """,
                (OVERVIEW_LIMIT,),
            )
            top_rows = cur.fetchall()
    return Response(render_overview_page(recent_rows, top_rows), mimetype="text/html")


@app.post("/recipes/upload")
def upload_recipe():
    payload = validate_recipe_payload(request.get_json(silent=True))
    if payload is None:
        return json_error("Invalid input", 400)

    title, ingredients, instructions = payload
    recipe_id = uuid.uuid4().hex

    with db_connection() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO recipes (id, title, ingredients, instructions)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (recipe_id, title, Json(ingredients), instructions),
                )

    response = {
        "id": recipe_id,
        "title": title,
        "ingredients": ingredients,
        "instructions": instructions,
        "comments": [],
        "avgRating": None,
    }
    return jsonify(response), 201, {"Location": f"/recipes/{recipe_id}"}


@app.get("/recipes/<string:recipe_id>")
def get_recipe(recipe_id: str) -> Response:
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, ingredients, instructions, avg_rating, rating_count
                FROM recipes
                WHERE id = %s
                """,
                (recipe_id,),
            )
            row = cur.fetchone()
            if row is None:
                return Response("Recipe not found", status=404, mimetype="text/plain")
            cur.execute(
                "SELECT comment FROM comments WHERE recipe_id = %s ORDER BY created_at ASC, id ASC",
                (recipe_id,),
            )
            comments = cur.fetchall()

    return Response(render_recipe_page(*row, comments), mimetype="text/html")


@app.post("/recipes/<string:recipe_id>/comments")
def add_comment(recipe_id: str):
    comment = validate_comment_payload(request.get_json(silent=True))
    if comment is None:
        return json_error("Invalid input", 400)

    with db_connection() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE recipes
                    SET comment_count = comment_count + 1, updated_at = NOW()
                    WHERE id = %s
                    RETURNING id
                    """,
                    (recipe_id,),
                )
                if cur.fetchone() is None:
                    return json_error("Recipe not found", 404)
                cur.execute(
                    "INSERT INTO comments (recipe_id, comment) VALUES (%s, %s)",
                    (recipe_id, comment),
                )

    return jsonify({"status": "created"}), 201


@app.post("/recipes/<string:recipe_id>/ratings")
def add_rating(recipe_id: str):
    rating = validate_rating_payload(request.get_json(silent=True))
    if rating is None:
        return json_error("Invalid input", 400)

    with db_connection() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE recipes
                    SET rating_sum = rating_sum + %s,
                        rating_count = rating_count + 1,
                        avg_rating = ((rating_sum + %s)::double precision / (rating_count + 1)),
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING id
                    """,
                    (rating, rating, recipe_id),
                )
                if cur.fetchone() is None:
                    return json_error("Recipe not found", 404)
                cur.execute(
                    "INSERT INTO ratings (recipe_id, rating) VALUES (%s, %s)",
                    (recipe_id, rating),
                )

    return jsonify({"status": "created"}), 201


try:
    ensure_database_ready()
except Exception as exc:
    app.logger.warning("Database initialization deferred: %s", exc)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5001")))
