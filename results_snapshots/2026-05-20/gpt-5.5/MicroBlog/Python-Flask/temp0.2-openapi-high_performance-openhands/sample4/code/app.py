import os
from contextlib import contextmanager
from datetime import timezone

import psycopg2
from psycopg2 import errors, pool
from flask import Flask, jsonify, request


PAGE_SIZE = 50
MAX_USERNAME_LENGTH = 255
SCHEMA_LOCK_KEY = 837465920143

app = Flask(__name__)
app.json.ensure_ascii = False

_pool = None
_pool_pid = None


def _db_config():
    return {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "user": os.environ.get("DB_USER", "postgres"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "dbname": os.environ.get("DB_NAME", "postgres"),
        "connect_timeout": 5,
        "application_name": "microblog_api",
        "options": "-c timezone=UTC",
    }


def _connect():
    return psycopg2.connect(**_db_config())


def init_db():
    conn = _connect()
    conn.autocommit = True
    cur = conn.cursor()
    locked = False
    try:
        cur.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_LOCK_KEY,))
        locked = True
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                full_name TEXT NOT NULL,
                bio TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CHECK (length(username) > 0 AND length(username) <= 255),
                CHECK (length(full_name) > 0)
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
                CHECK (length(content) > 0)
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
            """
            CREATE INDEX IF NOT EXISTS idx_posts_feed
            ON posts (username, created_at DESC, id DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_posts_trending
            ON posts (like_count DESC, id DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_follows_follower
            ON follows (follower_username, following_username)
            """
        )
    finally:
        if locked:
            try:
                cur.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK_KEY,))
            except Exception:
                pass
        cur.close()
        conn.close()


def get_pool():
    global _pool, _pool_pid
    pid = os.getpid()
    if _pool is None or _pool_pid != pid:
        maxconn = int(os.environ.get("DB_POOL_MAX", "8"))
        if maxconn < 1:
            maxconn = 1
        _pool = pool.ThreadedConnectionPool(1, maxconn, **_db_config())
        _pool_pid = pid
    return _pool


@contextmanager
def db_connection():
    p = get_pool()
    conn = p.getconn()
    try:
        yield conn
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        p.putconn(conn)


def error_response(message="Invalid input", status=400):
    return jsonify({"error": message}), status


def parse_json_object():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None
    return data


def required_string(data, name, *, max_length=None):
    value = data.get(name)
    if not isinstance(value, str) or value == "":
        raise ValueError(name)
    if max_length is not None and len(value) > max_length:
        raise ValueError(name)
    return value


def optional_string(data, name, default=""):
    value = data.get(name, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(name)
    return value


def parse_page():
    raw = request.args.get("page", "1")
    try:
        page = int(raw)
    except (TypeError, ValueError):
        raise ValueError("page")
    if page < 1:
        raise ValueError("page")
    return page


def format_datetime(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def feed_post(row):
    return {
        "id": row[0],
        "username": row[1],
        "content": row[2],
        "created_at": format_datetime(row[3]),
        "like_count": row[4],
    }


def trending_post(row):
    return {
        "id": row[0],
        "username": row[1],
        "content": row[2],
        "like_count": row[3],
    }


@app.post("/users")
def create_user():
    data = parse_json_object()
    if data is None:
        return error_response()
    try:
        username = required_string(data, "username", max_length=MAX_USERNAME_LENGTH)
        full_name = required_string(data, "full_name")
        bio = optional_string(data, "bio")
    except ValueError:
        return error_response()

    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (username, full_name, bio)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (username) DO NOTHING
                    RETURNING username
                    """,
                    (username, full_name, bio),
                )
                row = cur.fetchone()
                if row is None:
                    conn.rollback()
                    return error_response("Invalid input or username already exists", 400)
                conn.commit()
        return jsonify({"username": username, "full_name": full_name, "bio": bio}), 201
    except (psycopg2.DataError, psycopg2.IntegrityError):
        return error_response()


@app.post("/posts")
def create_post():
    data = parse_json_object()
    if data is None:
        return error_response()
    try:
        username = required_string(data, "username", max_length=MAX_USERNAME_LENGTH)
        content = required_string(data, "content")
    except ValueError:
        return error_response()

    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO posts (username, content)
                    VALUES (%s, %s)
                    RETURNING id, created_at
                    """,
                    (username, content),
                )
                post_id, created_at = cur.fetchone()
                conn.commit()
        return (
            jsonify(
                {
                    "id": post_id,
                    "username": username,
                    "content": content,
                    "created_at": format_datetime(created_at),
                    "like_count": 0,
                }
            ),
            201,
        )
    except (errors.ForeignKeyViolation, psycopg2.DataError, psycopg2.IntegrityError):
        return error_response()


@app.post("/follow")
def follow_user():
    data = parse_json_object()
    if data is None:
        return error_response()
    try:
        follower = required_string(data, "follower_username", max_length=MAX_USERNAME_LENGTH)
        following = required_string(data, "following_username", max_length=MAX_USERNAME_LENGTH)
    except ValueError:
        return error_response()

    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
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
                conn.commit()
        status = 201 if created else 200
        return jsonify({"follower_username": follower, "following_username": following}), status
    except (errors.ForeignKeyViolation, psycopg2.DataError, psycopg2.IntegrityError):
        return error_response()


@app.post("/posts/<int:post_id>/like")
def like_post(post_id):
    if post_id < 1:
        return error_response()
    data = parse_json_object()
    if data is None:
        return error_response()
    try:
        username = required_string(data, "username", max_length=MAX_USERNAME_LENGTH)
    except ValueError:
        return error_response()

    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
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
                    SELECT
                        COALESCE(
                            (SELECT like_count FROM updated),
                            (SELECT like_count FROM posts WHERE id = %s)
                        ) AS like_count,
                        EXISTS (SELECT 1 FROM inserted) AS created
                    """,
                    (post_id, username, post_id, post_id),
                )
                row = cur.fetchone()
                if row is None or row[0] is None:
                    conn.rollback()
                    return error_response()
                like_count, created = row
                conn.commit()
        return jsonify({"id": post_id, "like_count": like_count}), 201 if created else 200
    except (errors.ForeignKeyViolation, psycopg2.DataError, psycopg2.IntegrityError):
        return error_response()


@app.get("/feed")
def get_feed():
    username = request.args.get("username")
    if not isinstance(username, str) or username == "" or len(username) > MAX_USERNAME_LENGTH:
        return error_response()
    try:
        page = parse_page()
    except ValueError:
        return error_response()

    offset = (page - 1) * PAGE_SIZE
    with db_connection() as conn:
        with conn.cursor() as cur:
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
            conn.commit()
    has_next = len(rows) > PAGE_SIZE
    items = [feed_post(row) for row in rows[:PAGE_SIZE]]
    return jsonify({"items": items, "page": page, "page_size": PAGE_SIZE, "has_next": has_next})


@app.get("/trending")
def get_trending():
    try:
        page = parse_page()
    except ValueError:
        return error_response()

    offset = (page - 1) * PAGE_SIZE
    with db_connection() as conn:
        with conn.cursor() as cur:
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
            conn.commit()
    has_next = len(rows) > PAGE_SIZE
    items = [trending_post(row) for row in rows[:PAGE_SIZE]]
    return jsonify({"items": items, "page": page, "page_size": PAGE_SIZE, "has_next": has_next})


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


init_db()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False)
