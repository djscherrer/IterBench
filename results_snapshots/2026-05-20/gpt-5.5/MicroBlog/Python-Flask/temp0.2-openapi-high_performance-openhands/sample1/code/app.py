import os
import time
import threading
from contextlib import contextmanager
from datetime import timezone

import psycopg2
from psycopg2 import pool
from flask import Flask, jsonify, request


PAGE_SIZE = 50
SCHEMA_LOCK_ID = 732845901

app = Flask(__name__)
app.json.sort_keys = False

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
        "connect_timeout": 5,
    }


def _connect_with_retry(attempts=10, delay=0.5):
    last_error = None
    for _ in range(attempts):
        try:
            return psycopg2.connect(**_db_config())
        except psycopg2.OperationalError as exc:
            last_error = exc
            time.sleep(delay)
    raise last_error


def _init_schema():
    conn = _connect_with_retry()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_LOCK_ID,))
            try:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id BIGSERIAL PRIMARY KEY,
                        username VARCHAR(255) NOT NULL UNIQUE,
                        full_name TEXT NOT NULL,
                        bio TEXT NOT NULL DEFAULT '',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );

                    CREATE TABLE IF NOT EXISTS posts (
                        id BIGSERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        content TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        like_count INTEGER NOT NULL DEFAULT 0 CHECK (like_count >= 0)
                    );

                    CREATE TABLE IF NOT EXISTS follows (
                        follower_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        following_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (follower_id, following_id)
                    );

                    CREATE TABLE IF NOT EXISTS likes (
                        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (user_id, post_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_posts_user_created_id
                        ON posts (user_id, created_at DESC, id DESC);
                    CREATE INDEX IF NOT EXISTS idx_posts_trending
                        ON posts (like_count DESC, id DESC);
                    CREATE INDEX IF NOT EXISTS idx_follows_following
                        ON follows (following_id);
                    """
                )
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK_ID,))
    finally:
        conn.close()


def _get_pool():
    global _pool, _pool_pid
    pid = os.getpid()
    if _pool is not None and _pool_pid == pid:
        return _pool
    with _pool_lock:
        if _pool is None or _pool_pid != pid:
            if _pool is not None:
                try:
                    _pool.closeall()
                except Exception:
                    pass
            minconn = int(os.environ.get("DB_POOL_MIN", "1"))
            maxconn = int(os.environ.get("DB_POOL_MAX", "8"))
            _pool = pool.ThreadedConnectionPool(minconn, maxconn, **_db_config())
            _pool_pid = pid
        return _pool


@contextmanager
def _db_conn():
    db_pool = _get_pool()
    conn = db_pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        db_pool.putconn(conn)


def _error(message, status=400):
    return jsonify({"error": message}), status


def _json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None
    return data


def _required_string(data, key):
    value = data.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _format_datetime(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _parse_page():
    raw_page = request.args.get("page", "1")
    try:
        page = int(raw_page)
    except (TypeError, ValueError):
        return None
    return page if page >= 1 else None


@app.post("/users")
def create_user():
    data = _json_body()
    if data is None:
        return _error("Invalid JSON body")

    username = _required_string(data, "username")
    full_name = _required_string(data, "full_name")
    bio_value = data.get("bio", "")
    if username is None or full_name is None or not isinstance(bio_value, str):
        return _error("Invalid input")
    bio = bio_value.strip()

    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (username, full_name, bio)
                VALUES (%s, %s, %s)
                ON CONFLICT (username) DO NOTHING
                RETURNING id, username, full_name, bio
                """,
                (username, full_name, bio),
            )
            row = cur.fetchone()

    if row is None:
        return _error("Invalid input or username already exists")
    return jsonify({"id": row[0], "username": row[1], "full_name": row[2], "bio": row[3]}), 201


@app.post("/posts")
def create_post():
    data = _json_body()
    if data is None:
        return _error("Invalid JSON body")

    username = _required_string(data, "username")
    content = _required_string(data, "content")
    if username is None or content is None:
        return _error("Invalid input")

    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO posts (user_id, content)
                SELECT id, %s FROM users WHERE username = %s
                RETURNING id, content, created_at, like_count
                """,
                (content, username),
            )
            row = cur.fetchone()

    if row is None:
        return _error("Invalid input")
    return (
        jsonify(
            {
                "id": row[0],
                "username": username,
                "content": row[1],
                "created_at": _format_datetime(row[2]),
                "like_count": row[3],
            }
        ),
        201,
    )


@app.post("/follow")
def follow_user():
    data = _json_body()
    if data is None:
        return _error("Invalid JSON body")

    follower_username = _required_string(data, "follower_username")
    following_username = _required_string(data, "following_username")
    if follower_username is None or following_username is None:
        return _error("Invalid input")

    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH follower AS (
                    SELECT id FROM users WHERE username = %s
                ), following AS (
                    SELECT id FROM users WHERE username = %s
                ), inserted AS (
                    INSERT INTO follows (follower_id, following_id)
                    SELECT follower.id, following.id FROM follower, following
                    ON CONFLICT DO NOTHING
                    RETURNING 1
                )
                SELECT
                    EXISTS (SELECT 1 FROM follower) AS follower_exists,
                    EXISTS (SELECT 1 FROM following) AS following_exists,
                    EXISTS (SELECT 1 FROM inserted) AS inserted
                """,
                (follower_username, following_username),
            )
            follower_exists, following_exists, inserted = cur.fetchone()

    if not follower_exists or not following_exists:
        return _error("Invalid input")
    status = 201 if inserted else 200
    return jsonify({"follower_username": follower_username, "following_username": following_username}), status


@app.post("/posts/<int:post_id>/like")
def like_post(post_id):
    if post_id < 1:
        return _error("Invalid input")
    data = _json_body()
    if data is None:
        return _error("Invalid JSON body")

    username = _required_string(data, "username")
    if username is None:
        return _error("Invalid input")

    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH liker AS (
                    SELECT id FROM users WHERE username = %s
                ), target_post AS (
                    SELECT id FROM posts WHERE id = %s
                ), inserted AS (
                    INSERT INTO likes (user_id, post_id)
                    SELECT liker.id, target_post.id FROM liker, target_post
                    ON CONFLICT DO NOTHING
                    RETURNING 1
                ), updated AS (
                    UPDATE posts
                    SET like_count = like_count + 1
                    WHERE id = (SELECT id FROM target_post)
                      AND EXISTS (SELECT 1 FROM inserted)
                    RETURNING like_count
                )
                SELECT
                    EXISTS (SELECT 1 FROM liker) AS user_exists,
                    EXISTS (SELECT 1 FROM target_post) AS post_exists,
                    EXISTS (SELECT 1 FROM inserted) AS inserted,
                    COALESCE(
                        (SELECT like_count FROM updated),
                        (SELECT like_count FROM posts WHERE id = (SELECT id FROM target_post))
                    ) AS like_count
                """,
                (username, post_id),
            )
            user_exists, post_exists, inserted, like_count = cur.fetchone()

    if not user_exists or not post_exists:
        return _error("Invalid input")
    status = 201 if inserted else 200
    return jsonify({"id": post_id, "like_count": like_count}), status


@app.get("/feed")
def get_feed():
    username = request.args.get("username")
    if not isinstance(username, str) or not username.strip():
        return _error("Invalid input")
    username = username.strip()
    page = _parse_page()
    if page is None:
        return _error("Invalid input")

    offset = (page - 1) * PAGE_SIZE
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, u.username, p.content, p.created_at, p.like_count
                FROM follows f
                JOIN users follower ON follower.id = f.follower_id
                JOIN posts p ON p.user_id = f.following_id
                JOIN users u ON u.id = p.user_id
                WHERE follower.username = %s
                ORDER BY p.created_at DESC, p.id DESC
                LIMIT %s OFFSET %s
                """,
                (username, PAGE_SIZE + 1, offset),
            )
            rows = cur.fetchall()

    has_next = len(rows) > PAGE_SIZE
    items = [
        {
            "id": row[0],
            "username": row[1],
            "content": row[2],
            "created_at": _format_datetime(row[3]),
            "like_count": row[4],
        }
        for row in rows[:PAGE_SIZE]
    ]
    return jsonify({"items": items, "page": page, "page_size": PAGE_SIZE, "has_next": has_next})


@app.get("/trending")
def get_trending():
    page = _parse_page()
    if page is None:
        return _error("Invalid input")

    offset = (page - 1) * PAGE_SIZE
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, u.username, p.content, p.like_count
                FROM posts p
                JOIN users u ON u.id = p.user_id
                ORDER BY p.like_count DESC, p.id DESC
                LIMIT %s OFFSET %s
                """,
                (PAGE_SIZE + 1, offset),
            )
            rows = cur.fetchall()

    has_next = len(rows) > PAGE_SIZE
    items = [
        {"id": row[0], "username": row[1], "content": row[2], "like_count": row[3]}
        for row in rows[:PAGE_SIZE]
    ]
    return jsonify({"items": items, "page": page, "page_size": PAGE_SIZE, "has_next": has_next})


@app.get("/")
def health():
    return jsonify({"status": "ok"})


_init_schema()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
