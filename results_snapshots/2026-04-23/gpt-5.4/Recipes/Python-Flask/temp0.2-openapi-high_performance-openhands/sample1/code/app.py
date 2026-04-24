import os
import socket
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

from flask import Flask, Response, jsonify, render_template_string, request
from psycopg2 import connect
from psycopg2.pool import ThreadedConnectionPool


app = Flask(__name__)

OVERVIEW_CACHE_TTL = float(os.getenv("OVERVIEW_CACHE_TTL", "2.0"))
SCHEMA_LOCK_ID = 68427130591822341

_pool = None
_pool_pid = None
_pool_lock = threading.Lock()
_overview_cache = {"expires_at": 0.0, "html": None}
_overview_cache_lock = threading.Lock()

OVERVIEW_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Recipe Overview</title>
    <style>
      body { font-family: Arial, sans-serif; margin: 2rem auto; max-width: 900px; line-height: 1.5; }
      h1, h2 { margin-bottom: 0.5rem; }
      .grid { display: grid; gap: 2rem; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
      ul { padding-left: 1.2rem; }
      li { margin: 0.5rem 0; }
      .meta { color: #555; font-size: 0.95rem; }
      a { color: #0b57d0; text-decoration: none; }
      a:hover { text-decoration: underline; }
    </style>
  </head>
  <body>
    <h1>Recipe Sharing App</h1>
    <div class="grid">
      <section>
        <h2>Recent Recipes</h2>
        {% if recent_recipes %}
          <ul>
            {% for recipe in recent_recipes %}
              <li>
                <a href="/recipes/{{ recipe.id }}">{{ recipe.title }}</a>
                <div class="meta">Created {{ recipe.created_at }}</div>
              </li>
            {% endfor %}
          </ul>
        {% else %}
          <p>No recipes uploaded yet.</p>
        {% endif %}
      </section>
      <section>
        <h2>Top Rated Recipes</h2>
        {% if top_recipes %}
          <ul>
            {% for recipe in top_recipes %}
              <li>
                <a href="/recipes/{{ recipe.id }}">{{ recipe.title }}</a>
                <div class="meta">Average rating: {{ recipe.avg_rating }} ({{ recipe.rating_count }} ratings)</div>
              </li>
            {% endfor %}
          </ul>
        {% else %}
          <p>No ratings yet.</p>
        {% endif %}
      </section>
    </div>
  </body>
</html>
"""

DETAIL_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ recipe.title }}</title>
    <style>
      body { font-family: Arial, sans-serif; margin: 2rem auto; max-width: 900px; line-height: 1.6; }
      h1, h2 { margin-bottom: 0.5rem; }
      .meta { color: #555; }
      ul { padding-left: 1.2rem; }
      li { margin: 0.35rem 0; }
      .comment { border-bottom: 1px solid #ddd; padding: 0.75rem 0; }
      .back { margin-bottom: 1rem; display: inline-block; }
    </style>
  </head>
  <body>
    <a class="back" href="/recipes">&larr; Back to overview</a>
    <h1>{{ recipe.title }}</h1>
    <p class="meta">
      Average rating:
      {% if recipe.avg_rating is not none %}
        {{ recipe.avg_rating }} from {{ recipe.rating_count }} rating(s)
      {% else %}
        No ratings yet
      {% endif %}
      · {{ recipe.comment_count }} comment(s)
    </p>

    <section>
      <h2>Ingredients</h2>
      <ul>
        {% for ingredient in recipe.ingredients %}
          <li>{{ ingredient }}</li>
        {% endfor %}
      </ul>
    </section>

    <section>
      <h2>Instructions</h2>
      <p>{{ recipe.instructions }}</p>
    </section>

    <section>
      <h2>Comments</h2>
      {% if comments %}
        {% for item in comments %}
          <div class="comment">
            <div>{{ item.comment }}</div>
            <div class="meta">{{ item.created_at }}</div>
          </div>
        {% endfor %}
      {% else %}
        <p>No comments yet.</p>
      {% endif %}
    </section>
  </body>
</html>
"""


def resolve_db_host(host, port):
    if not host:
        return "localhost"
    try:
        socket.getaddrinfo(host, port)
        return host
    except socket.gaierror:
        fallback = os.getenv("DB_HOST_FALLBACK")
        return fallback or "127.0.0.1"


def db_settings():
    port = int(os.getenv("DB_PORT", "5432"))
    return {
        "host": resolve_db_host(os.getenv("DB_HOST", "localhost"), port),
        "port": port,
        "user": os.getenv("DB_USER", "postgres"),
        "password": os.getenv("DB_PASSWORD", "postgres"),
        "dbname": os.getenv("DB_NAME", "postgres"),
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "5")),
    }


@contextmanager
def get_db_connection():
    pool = get_pool()
    conn = pool.getconn()
    conn.autocommit = False
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        pool.putconn(conn)


def get_pool():
    global _pool, _pool_pid

    current_pid = os.getpid()
    if _pool is not None and _pool_pid == current_pid:
        return _pool

    with _pool_lock:
        if _pool is not None and _pool_pid == current_pid:
            return _pool

        if _pool is not None and _pool_pid != current_pid:
            try:
                _pool.closeall()
            except Exception:
                pass
            _pool = None
            _pool_pid = None

        maxconn = int(os.getenv("DB_POOL_MAX", str(max(4, (os.cpu_count() or 1) * 2))))
        _pool = ThreadedConnectionPool(1, maxconn, **db_settings())
        _pool_pid = current_pid
        return _pool


def invalidate_overview_cache():
    with _overview_cache_lock:
        _overview_cache["expires_at"] = 0.0
        _overview_cache["html"] = None


def init_db():
    conn = connect(**db_settings())
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_LOCK_ID,))
            try:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS recipes (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        ingredients TEXT[] NOT NULL,
                        instructions TEXT NOT NULL,
                        rating_sum INTEGER NOT NULL DEFAULT 0,
                        rating_count INTEGER NOT NULL DEFAULT 0,
                        comment_count INTEGER NOT NULL DEFAULT 0,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS recipe_comments (
                        id BIGSERIAL PRIMARY KEY,
                        recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                        comment TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS recipe_ratings (
                        id BIGSERIAL PRIMARY KEY,
                        recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                        rating SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute("CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes (created_at DESC)")
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_recipes_top_rated
                    ON recipes (((rating_sum::NUMERIC / NULLIF(rating_count, 0))) DESC, rating_count DESC, created_at DESC)
                    WHERE rating_count > 0
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_recipe_comments_recipe_created ON recipe_comments (recipe_id, created_at DESC)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_recipe_ratings_recipe_created ON recipe_ratings (recipe_id, created_at DESC)"
                )
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK_ID,))
    finally:
        conn.close()


init_db()


def format_timestamp(value):
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")


def validate_json_object():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None
    return payload


def normalize_string(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def build_recipe_response(recipe_id, title, ingredients, instructions):
    return {
        "id": recipe_id,
        "title": title,
        "ingredients": ingredients,
        "instructions": instructions,
        "comments": [],
        "avgRating": None,
    }


@app.get("/recipes")
def list_recipes():
    now = time.monotonic()
    with _overview_cache_lock:
        if _overview_cache["html"] is not None and _overview_cache["expires_at"] > now:
            return Response(_overview_cache["html"], mimetype="text/html")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, created_at
                FROM recipes
                ORDER BY created_at DESC
                LIMIT 10
                """
            )
            recent_rows = cur.fetchall()
            cur.execute(
                """
                SELECT
                    id,
                    title,
                    ROUND((rating_sum::NUMERIC / NULLIF(rating_count, 0)), 2) AS avg_rating,
                    rating_count
                FROM recipes
                WHERE rating_count > 0
                ORDER BY (rating_sum::NUMERIC / rating_count) DESC, rating_count DESC, created_at DESC
                LIMIT 10
                """
            )
            top_rows = cur.fetchall()

    recent_recipes = [
        {"id": row[0], "title": row[1], "created_at": format_timestamp(row[2])}
        for row in recent_rows
    ]
    top_recipes = [
        {"id": row[0], "title": row[1], "avg_rating": float(row[2]), "rating_count": row[3]}
        for row in top_rows
    ]
    html = render_template_string(
        OVERVIEW_TEMPLATE,
        recent_recipes=recent_recipes,
        top_recipes=top_recipes,
    )

    with _overview_cache_lock:
        _overview_cache["html"] = html
        _overview_cache["expires_at"] = time.monotonic() + OVERVIEW_CACHE_TTL

    return Response(html, mimetype="text/html")


@app.post("/recipes/upload")
def upload_recipe():
    payload = validate_json_object()
    if payload is None:
        return jsonify({"error": "Invalid JSON body"}), 400

    title = normalize_string(payload.get("title"))
    instructions = normalize_string(payload.get("instructions"))
    ingredients = payload.get("ingredients")

    if title is None or instructions is None or not isinstance(ingredients, list):
        return jsonify({"error": "Invalid input"}), 400

    normalized_ingredients = []
    for item in ingredients:
        ingredient = normalize_string(item)
        if ingredient is None:
            return jsonify({"error": "Invalid input"}), 400
        normalized_ingredients.append(ingredient)

    recipe_id = str(uuid4())
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO recipes (id, title, ingredients, instructions)
                VALUES (%s, %s, %s, %s)
                """,
                (recipe_id, title, normalized_ingredients, instructions),
            )
        conn.commit()

    invalidate_overview_cache()
    return jsonify(build_recipe_response(recipe_id, title, normalized_ingredients, instructions)), 201


@app.get("/recipes/<string:recipe_id>")
def get_recipe(recipe_id):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    title,
                    ingredients,
                    instructions,
                    CASE
                        WHEN rating_count > 0 THEN ROUND((rating_sum::NUMERIC / rating_count), 2)
                        ELSE NULL
                    END AS avg_rating,
                    rating_count,
                    comment_count
                FROM recipes
                WHERE id = %s
                """,
                (recipe_id,),
            )
            recipe_row = cur.fetchone()
            if recipe_row is None:
                return jsonify({"error": "Recipe not found"}), 404

            cur.execute(
                """
                SELECT comment, created_at
                FROM recipe_comments
                WHERE recipe_id = %s
                ORDER BY created_at DESC
                LIMIT 100
                """,
                (recipe_id,),
            )
            comment_rows = cur.fetchall()

    recipe = {
        "id": recipe_row[0],
        "title": recipe_row[1],
        "ingredients": list(recipe_row[2]),
        "instructions": recipe_row[3],
        "avg_rating": float(recipe_row[4]) if recipe_row[4] is not None else None,
        "rating_count": recipe_row[5],
        "comment_count": recipe_row[6],
    }
    comments = [{"comment": row[0], "created_at": format_timestamp(row[1])} for row in comment_rows]
    return Response(render_template_string(DETAIL_TEMPLATE, recipe=recipe, comments=comments), mimetype="text/html")


@app.post("/recipes/<string:recipe_id>/comments")
def add_comment(recipe_id):
    payload = validate_json_object()
    if payload is None:
        return jsonify({"error": "Invalid JSON body"}), 400

    comment = normalize_string(payload.get("comment"))
    if comment is None:
        return jsonify({"error": "Invalid input"}), 400

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE recipes SET comment_count = comment_count + 1 WHERE id = %s RETURNING id",
                (recipe_id,),
            )
            if cur.fetchone() is None:
                conn.rollback()
                return jsonify({"error": "Recipe not found"}), 404
            cur.execute(
                "INSERT INTO recipe_comments (recipe_id, comment) VALUES (%s, %s)",
                (recipe_id, comment),
            )
        conn.commit()

    invalidate_overview_cache()
    return jsonify({"status": "created"}), 201


@app.post("/recipes/<string:recipe_id>/ratings")
def add_rating(recipe_id):
    payload = validate_json_object()
    if payload is None:
        return jsonify({"error": "Invalid JSON body"}), 400

    rating = payload.get("rating")
    if isinstance(rating, bool) or not isinstance(rating, int) or rating < 1 or rating > 5:
        return jsonify({"error": "Invalid input"}), 400

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE recipes
                SET rating_sum = rating_sum + %s, rating_count = rating_count + 1
                WHERE id = %s
                RETURNING id
                """,
                (rating, recipe_id),
            )
            if cur.fetchone() is None:
                conn.rollback()
                return jsonify({"error": "Recipe not found"}), 404
            cur.execute(
                "INSERT INTO recipe_ratings (recipe_id, rating) VALUES (%s, %s)",
                (recipe_id, rating),
            )
        conn.commit()

    invalidate_overview_cache()
    return jsonify({"status": "created"}), 201


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5001"))
    app.run(host="0.0.0.0", port=port)
