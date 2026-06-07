import os
import threading
from contextlib import contextmanager
from datetime import timezone

import psycopg2
from psycopg2 import IntegrityError
from psycopg2.pool import ThreadedConnectionPool
from flask import Flask, jsonify, request


PAGE_SIZE = 50
_POOL = None
_POOL_PID = None
_POOL_LOCK = threading.Lock()

app = Flask(__name__)
app.json.sort_keys = False


def _db_config():
    return {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": os.environ.get("DB_PORT", "5432"),
        "user": os.environ.get("DB_USER", "postgres"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "dbname": os.environ.get("DB_NAME", "postgres"),
        "connect_timeout": int(os.environ.get("DB_CONNECT_TIMEOUT", "5")),
        "application_name": "microblog_api",
    }


def _new_connection():
    return psycopg2.connect(**_db_config())


def init_db():
    conn = _new_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (917260513,))
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        username TEXT PRIMARY KEY,
                        full_name TEXT NOT NULL,
                        bio TEXT NOT NULL DEFAULT '',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        CHECK (length(btrim(username)) > 0),
                        CHECK (length(btrim(full_name)) > 0)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS posts (
                        id BIGSERIAL PRIMARY KEY,
                        username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                        content TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        like_count INTEGER NOT NULL DEFAULT 0 CHECK (like_count >= 0),
                        CHECK (length(btrim(content)) > 0)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS follows (
                        follower_username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                        following_username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (follower_username, following_username)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS post_likes (
                        post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                        username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (post_id, username)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_posts_username_created_id "
                    "ON posts (username, created_at DESC, id DESC)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_posts_like_count_id "
                    "ON posts (like_count DESC, id DESC)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_follows_follower_following "
                    "ON follows (follower_username, following_username)"
                )
    finally:
        conn.close()

def _get_pool():
    global _POOL, _POOL_PID
    pid = os.getpid()
    if _POOL is None or _POOL_PID != pid:
        with _POOL_LOCK:
            if _POOL is None or _POOL_PID != pid:
                maxconn = max(1, int(os.environ.get("DB_POOL_MAX", "10")))
                minconn = min(maxconn, max(1, int(os.environ.get("DB_POOL_MIN", "1"))))
                _POOL = ThreadedConnectionPool(minconn, maxconn, **_db_config())
                _POOL_PID = pid
    return _POOL


@contextmanager
def db_cursor():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn:
            with conn.cursor() as cur:
                yield cur
    finally:
        if conn.closed:
            pool.putconn(conn, close=True)
        else:
            pool.putconn(conn)


def _json_error(message, status=400):
    return jsonify({"error": message}), status


def _payload():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _nonempty_string(value):
    return isinstance(value, str) and value.strip() != ""


def _optional_string(value):
    return value is None or isinstance(value, str)


def _parse_page():
    raw = request.args.get("page", "1")
    try:
        page = int(raw)
    except (TypeError, ValueError):
        return None
    return page if page >= 1 else None


def _format_dt(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _post_with_time(row):
    post_id, username, content, created_at, like_count = row
    return {
        "id": int(post_id),
        "username": username,
        "content": content,
        "created_at": _format_dt(created_at),
        "like_count": int(like_count),
    }


def _post_trending(row):
    post_id, username, content, like_count = row
    return {
        "id": int(post_id),
        "username": username,
        "content": content,
        "like_count": int(like_count),
    }


@app.post("/users")
def create_user():
    data = _payload()
    if data is None:
        return _json_error("Invalid JSON body")

    username = data.get("username")
    full_name = data.get("full_name")
    bio = data.get("bio", "")
    if not _nonempty_string(username) or not _nonempty_string(full_name) or not _optional_string(bio):
        return _json_error("Invalid input")

    try:
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, full_name, bio) VALUES (%s, %s, %s)",
                (username, full_name, bio or ""),
            )
        return jsonify({"username": username, "full_name": full_name, "bio": bio or ""}), 201
    except IntegrityError:
        return _json_error("Invalid input or username already exists")


@app.post("/posts")
def create_post():
    data = _payload()
    if data is None:
        return _json_error("Invalid JSON body")

    username = data.get("username")
    content = data.get("content")
    if not _nonempty_string(username) or not _nonempty_string(content):
        return _json_error("Invalid input")

    try:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO posts (username, content)
                VALUES (%s, %s)
                RETURNING id, username, content, created_at, like_count
                """,
                (username, content),
            )
            item = _post_with_time(cur.fetchone())
        return jsonify(item), 201
    except IntegrityError:
        return _json_error("Invalid input")


@app.post("/follow")
def follow_user():
    data = _payload()
    if data is None:
        return _json_error("Invalid JSON body")

    follower = data.get("follower_username")
    following = data.get("following_username")
    if not _nonempty_string(follower) or not _nonempty_string(following):
        return _json_error("Invalid input")

    try:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO follows (follower_username, following_username)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                RETURNING follower_username
                """,
                (follower, following),
            )
            created = cur.fetchone() is not None
        return ("", 201) if created else ("", 200)
    except IntegrityError:
        return _json_error("Invalid input")


@app.post("/posts/<post_id>/like")
def like_post(post_id):
    try:
        post_id = int(post_id)
    except (TypeError, ValueError):
        return _json_error("Invalid input")
    if post_id <= 0:
        return _json_error("Invalid input")

    data = _payload()
    if data is None:
        return _json_error("Invalid JSON body")

    username = data.get("username")
    if not _nonempty_string(username):
        return _json_error("Invalid input")

    try:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO post_likes (post_id, username)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                RETURNING post_id
                """,
                (post_id, username),
            )
            created = cur.fetchone() is not None
            like_count = None
            if created:
                cur.execute(
                    "UPDATE posts SET like_count = like_count + 1 WHERE id = %s RETURNING like_count",
                    (post_id,),
                )
                row = cur.fetchone()
                like_count = int(row[0]) if row else None
        body = {"post_id": post_id, "username": username}
        if like_count is not None:
            body["like_count"] = like_count
        return jsonify(body), 201 if created else 200
    except IntegrityError:
        return _json_error("Invalid input")


@app.get("/feed")
def get_feed():
    username = request.args.get("username")
    page = _parse_page()
    if not _nonempty_string(username) or page is None:
        return _json_error("Invalid input")

    offset = (page - 1) * PAGE_SIZE
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT p.id, p.username, p.content, p.created_at, p.like_count
            FROM follows f
            JOIN posts p ON p.username = f.following_username
            WHERE f.follower_username = %s
            ORDER BY p.created_at DESC, p.id DESC
            LIMIT %s OFFSET %s
            """,
            (username, PAGE_SIZE + 1, offset),
        )
        rows = cur.fetchall()

    return jsonify(
        {
            "items": [_post_with_time(row) for row in rows[:PAGE_SIZE]],
            "page": page,
            "page_size": PAGE_SIZE,
            "has_next": len(rows) > PAGE_SIZE,
        }
    )


@app.get("/trending")
def get_trending():
    page = _parse_page()
    if page is None:
        return _json_error("Invalid input")

    offset = (page - 1) * PAGE_SIZE
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id, username, content, like_count
            FROM posts
            ORDER BY like_count DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            (PAGE_SIZE + 1, offset),
        )
        rows = cur.fetchall()

    return jsonify(
        {
            "items": [_post_trending(row) for row in rows[:PAGE_SIZE]],
            "page": page,
            "page_size": PAGE_SIZE,
            "has_next": len(rows) > PAGE_SIZE,
        }
    )


init_db()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
