import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
from psycopg2 import IntegrityError, OperationalError
from psycopg2.pool import ThreadedConnectionPool
from flask import Flask, request


PAGE_SIZE = 50
SCHEMA_LOCK_KEY = 642318947
MAX_USERNAME_LENGTH = 64
MAX_FULL_NAME_LENGTH = 255
MAX_BIO_LENGTH = 4096
MAX_CONTENT_LENGTH = 10000

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = Flask(__name__)

_pool = None
_pool_pid = None
_pool_lock = threading.Lock()


def _db_config():
    return {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "user": os.environ.get("DB_USER", "postgres"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "dbname": os.environ.get("DB_NAME", "postgres"),
        "connect_timeout": int(os.environ.get("DB_CONNECT_TIMEOUT", "5")),
        "application_name": f"microblog_api_{os.getpid()}",
        "options": "-c timezone=UTC",
    }


def _create_connection():
    return psycopg2.connect(**_db_config())


def init_db():
    conn = _create_connection()
    conn.autocommit = False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_KEY,))
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        username TEXT PRIMARY KEY,
                        full_name TEXT NOT NULL,
                        bio TEXT NOT NULL DEFAULT '',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        CHECK (char_length(username) BETWEEN 1 AND 64),
                        CHECK (char_length(full_name) BETWEEN 1 AND 255),
                        CHECK (char_length(bio) <= 4096)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS posts (
                        id BIGSERIAL PRIMARY KEY,
                        username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                        content TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        like_count INTEGER NOT NULL DEFAULT 0 CHECK (like_count >= 0),
                        CHECK (char_length(content) BETWEEN 1 AND 10000)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS follows (
                        follower_username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                        following_username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (follower_username, following_username)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS likes (
                        post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                        username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (post_id, username)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_posts_feed_order ON posts (username, created_at DESC, id DESC)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_posts_trending_order ON posts (like_count DESC, id DESC)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_follows_following ON follows (following_username)"
                )
                cur.execute("CREATE INDEX IF NOT EXISTS idx_likes_username ON likes (username)")
    finally:
        conn.close()


def _get_pool():
    global _pool, _pool_pid
    pid = os.getpid()
    if _pool is not None and _pool_pid == pid:
        return _pool
    with _pool_lock:
        if _pool is not None and _pool_pid == pid:
            return _pool
        if _pool is not None:
            try:
                _pool.closeall()
            except Exception:
                logger.exception("Failed to close inherited database pool")
        max_connections = max(1, int(os.environ.get("DB_POOL_MAX", "8")))
        _pool = ThreadedConnectionPool(1, max_connections, **_db_config())
        _pool_pid = pid
        return _pool


@contextmanager
def db_cursor():
    pool = _get_pool()
    conn = pool.getconn()
    close_conn = False
    try:
        with conn:
            with conn.cursor() as cur:
                yield cur
    except OperationalError:
        close_conn = True
        raise
    finally:
        if conn.closed:
            close_conn = True
        pool.putconn(conn, close=close_conn)


def json_response(payload, status=200):
    return app.response_class(
        json.dumps(payload, separators=(",", ":")),
        status=status,
        mimetype="application/json",
    )


def error_response(message, status=400):
    return json_response({"error": message}, status)


def _body_json():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None
    return data


def _required_string(data, name, max_length=None, strip_value=True):
    value = data.get(name)
    if not isinstance(value, str):
        return None
    if not value.strip():
        return None
    result = value.strip() if strip_value else value
    if max_length is not None and len(result) > max_length:
        return None
    return result


def _optional_string(data, name, max_length=None, strip_value=True):
    value = data.get(name, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        return None
    result = value.strip() if strip_value else value
    if max_length is not None and len(result) > max_length:
        return None
    return result


def _parse_page():
    raw_page = request.args.get("page", "1")
    try:
        page = int(raw_page)
    except (TypeError, ValueError):
        return None
    if page < 1:
        return None
    return page


def _format_dt(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    return str(value)


def _post_with_created(row):
    return {
        "id": row[0],
        "username": row[1],
        "content": row[2],
        "created_at": _format_dt(row[3]),
        "like_count": row[4],
    }


def _post_without_created(row):
    return {"id": row[0], "username": row[1], "content": row[2], "like_count": row[3]}


@app.post("/users")
def create_user():
    data = _body_json()
    if data is None:
        return error_response("JSON request body is required")
    username = _required_string(data, "username", MAX_USERNAME_LENGTH)
    full_name = _required_string(data, "full_name", MAX_FULL_NAME_LENGTH)
    bio = _optional_string(data, "bio", MAX_BIO_LENGTH)
    if username is None or full_name is None or bio is None:
        return error_response("Invalid input")

    try:
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, full_name, bio) VALUES (%s, %s, %s)",
                (username, full_name, bio),
            )
    except IntegrityError:
        return error_response("Invalid input or username already exists")
    return json_response({"username": username, "full_name": full_name, "bio": bio}, 201)


@app.post("/posts")
def create_post():
    data = _body_json()
    if data is None:
        return error_response("JSON request body is required")
    username = _required_string(data, "username", MAX_USERNAME_LENGTH)
    content = _required_string(data, "content", MAX_CONTENT_LENGTH, strip_value=False)
    if username is None or content is None:
        return error_response("Invalid input")

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
            row = cur.fetchone()
    except IntegrityError:
        return error_response("Invalid input")
    return json_response(_post_with_created(row), 201)


@app.post("/follow")
def follow_user():
    data = _body_json()
    if data is None:
        return error_response("JSON request body is required")
    follower = _required_string(data, "follower_username", MAX_USERNAME_LENGTH)
    following = _required_string(data, "following_username", MAX_USERNAME_LENGTH)
    if follower is None or following is None:
        return error_response("Invalid input")

    try:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO follows (follower_username, following_username)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                RETURNING 1
                """,
                (follower, following),
            )
            created = cur.fetchone() is not None
    except IntegrityError:
        return error_response("Invalid input")
    if created:
        return json_response({"status": "followed"}, 201)
    return json_response({"status": "already_following"}, 200)


@app.post("/posts/<post_id>/like")
def like_post(post_id):
    try:
        post_id_int = int(post_id)
    except (TypeError, ValueError):
        return error_response("Invalid input")
    if post_id_int < 1:
        return error_response("Invalid input")

    data = _body_json()
    if data is None:
        return error_response("JSON request body is required")
    username = _required_string(data, "username", MAX_USERNAME_LENGTH)
    if username is None:
        return error_response("Invalid input")

    try:
        with db_cursor() as cur:
            cur.execute(
                """
                WITH inserted AS (
                    INSERT INTO likes (post_id, username)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING 1
                ), updated AS (
                    UPDATE posts
                    SET like_count = like_count + 1
                    WHERE id = %s AND EXISTS (SELECT 1 FROM inserted)
                    RETURNING like_count
                )
                SELECT EXISTS (SELECT 1 FROM inserted)
                """,
                (post_id_int, username, post_id_int),
            )
            created = cur.fetchone()[0]
    except IntegrityError:
        return error_response("Invalid input")
    if created:
        return json_response({"status": "liked"}, 201)
    return json_response({"status": "already_liked"}, 200)


@app.get("/feed")
def get_feed():
    username = request.args.get("username")
    if not isinstance(username, str) or not username.strip():
        return error_response("username query parameter is required")
    username = username.strip()
    if len(username) > MAX_USERNAME_LENGTH:
        return error_response("Invalid input")
    page = _parse_page()
    if page is None:
        return error_response("Invalid page")
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
    items = [_post_with_created(row) for row in rows[:PAGE_SIZE]]
    return json_response(
        {"items": items, "page": page, "page_size": PAGE_SIZE, "has_next": len(rows) > PAGE_SIZE}
    )


@app.get("/trending")
def get_trending():
    page = _parse_page()
    if page is None:
        return error_response("Invalid page")
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
    items = [_post_without_created(row) for row in rows[:PAGE_SIZE]]
    return json_response(
        {"items": items, "page": page, "page_size": PAGE_SIZE, "has_next": len(rows) > PAGE_SIZE}
    )


@app.get("/health")
def health():
    return json_response({"status": "ok"})


@app.errorhandler(404)
def not_found(_error):
    return error_response("Not found", 404)


@app.errorhandler(405)
def method_not_allowed(_error):
    return error_response("Method not allowed", 405)


@app.errorhandler(Exception)
def internal_error(error):
    logger.exception("Unhandled application error: %s", error)
    return error_response("Internal server error", 500)


init_db()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
