import os
import time
import threading
from contextlib import contextmanager
from datetime import timezone

import psycopg2
from psycopg2 import pool
from flask import Flask, jsonify, request


PAGE_SIZE = 50
SCHEMA_LOCK_ID = 774291031

app = Flask(__name__)
app.json.compact = True

_db_pool = None
_db_pool_pid = None
_db_pool_lock = threading.Lock()


def _db_config():
    return {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "user": os.environ.get("DB_USER", "postgres"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "dbname": os.environ.get("DB_NAME", "postgres"),
        "connect_timeout": 5,
        "application_name": "microblog_api",
    }


def _connect():
    return psycopg2.connect(**_db_config())


def init_db():
    last_error = None
    for attempt in range(5):
        conn = None
        try:
            conn = _connect()
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_LOCK_ID,))
                try:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS users (
                            id BIGSERIAL PRIMARY KEY,
                            username TEXT NOT NULL UNIQUE,
                            full_name TEXT NOT NULL,
                            bio TEXT,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS posts (
                            id BIGSERIAL PRIMARY KEY,
                            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                            content TEXT NOT NULL,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            like_count INTEGER NOT NULL DEFAULT 0 CHECK (like_count >= 0)
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS follows (
                            follower_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                            following_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            PRIMARY KEY (follower_id, following_id)
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS likes (
                            post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            PRIMARY KEY (post_id, user_id)
                        )
                        """
                    )
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS idx_posts_user_created_id "
                        "ON posts (user_id, created_at DESC, id DESC)"
                    )
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS idx_posts_trending "
                        "ON posts (like_count DESC, id DESC)"
                    )
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS idx_follows_following "
                        "ON follows (following_id)"
                    )
                finally:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK_ID,))
            conn.commit()
            return
        except psycopg2.Error as exc:
            last_error = exc
            if conn is not None:
                conn.rollback()
            if attempt == 4:
                raise
            time.sleep(0.25 * (attempt + 1))
        finally:
            if conn is not None:
                conn.close()
    if last_error is not None:
        raise last_error


def get_pool():
    global _db_pool, _db_pool_pid
    pid = os.getpid()
    if _db_pool is None or _db_pool_pid != pid:
        with _db_pool_lock:
            if _db_pool is None or _db_pool_pid != pid:
                maxconn = max(1, int(os.environ.get("DB_POOL_MAX", "4")))
                _db_pool = pool.ThreadedConnectionPool(1, maxconn, **_db_config())
                _db_pool_pid = pid
    return _db_pool


@contextmanager
def db_cursor():
    conn = get_pool().getconn()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            yield cur
    finally:
        get_pool().putconn(conn)


def json_error(message, status=400):
    return jsonify({"error": message}), status


def json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None
    return data


def required_string(data, key, *, strip=True, max_length=None):
    value = data.get(key)
    if not isinstance(value, str):
        return None
    normalized = value.strip() if strip else value
    if normalized == "":
        return None
    if max_length is not None and len(normalized) > max_length:
        return None
    return normalized


def optional_string(data, key, *, max_length=None):
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if not isinstance(value, str):
        return None
    if max_length is not None and len(value) > max_length:
        return None
    return value


def parse_page():
    raw_page = request.args.get("page", "1")
    try:
        page = int(raw_page)
    except (TypeError, ValueError):
        return None
    if page < 1:
        return None
    return page


def parse_positive_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed < 1:
        return None
    return parsed


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
    data = json_body()
    if data is None:
        return json_error("JSON object body required")

    username = required_string(data, "username", max_length=255)
    full_name = required_string(data, "full_name", max_length=255)
    bio = optional_string(data, "bio", max_length=4096)
    if username is None or full_name is None or ("bio" in data and data["bio"] is not None and bio is None):
        return json_error("Invalid input")

    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (username, full_name, bio)
            VALUES (%s, %s, %s)
            ON CONFLICT (username) DO NOTHING
            RETURNING id
            """,
            (username, full_name, bio),
        )
        row = cur.fetchone()

    if row is None:
        return json_error("Invalid input or username already exists")
    return jsonify({"id": row[0], "username": username, "full_name": full_name, "bio": bio}), 201


@app.post("/posts")
def create_post():
    data = json_body()
    if data is None:
        return json_error("JSON object body required")

    username = required_string(data, "username", max_length=255)
    content = required_string(data, "content", strip=False, max_length=10000)
    if username is None or content is None or content.strip() == "":
        return json_error("Invalid input")

    with db_cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        user_row = cur.fetchone()
        if user_row is None:
            return json_error("Invalid input")
        cur.execute(
            """
            INSERT INTO posts (user_id, content)
            VALUES (%s, %s)
            RETURNING id, created_at, like_count
            """,
            (user_row[0], content),
        )
        row = cur.fetchone()

    return (
        jsonify(
            {
                "id": row[0],
                "username": username,
                "content": content,
                "created_at": format_datetime(row[1]),
                "like_count": row[2],
            }
        ),
        201,
    )


@app.post("/follow")
def follow_user():
    data = json_body()
    if data is None:
        return json_error("JSON object body required")

    follower_username = required_string(data, "follower_username", max_length=255)
    following_username = required_string(data, "following_username", max_length=255)
    if follower_username is None or following_username is None:
        return json_error("Invalid input")

    with db_cursor() as cur:
        cur.execute(
            """
            WITH follower AS (
                SELECT id FROM users WHERE username = %s
            ), following AS (
                SELECT id FROM users WHERE username = %s
            ), ins AS (
                INSERT INTO follows (follower_id, following_id)
                SELECT follower.id, following.id
                FROM follower CROSS JOIN following
                ON CONFLICT DO NOTHING
                RETURNING 1
            )
            SELECT
                EXISTS (SELECT 1 FROM follower) AS follower_exists,
                EXISTS (SELECT 1 FROM following) AS following_exists,
                EXISTS (SELECT 1 FROM ins) AS inserted
            """,
            (follower_username, following_username),
        )
        follower_exists, following_exists, inserted = cur.fetchone()

    if not follower_exists or not following_exists:
        return json_error("Invalid input")
    if inserted:
        return jsonify({"status": "followed"}), 201
    return jsonify({"status": "already_following"}), 200


@app.post("/posts/<post_id>/like")
def like_post(post_id):
    parsed_post_id = parse_positive_int(post_id)
    if parsed_post_id is None:
        return json_error("Invalid input")

    data = json_body()
    if data is None:
        return json_error("JSON object body required")
    username = required_string(data, "username", max_length=255)
    if username is None:
        return json_error("Invalid input")

    with db_cursor() as cur:
        cur.execute(
            """
            WITH liker AS (
                SELECT id FROM users WHERE username = %s
            ), post_row AS (
                SELECT id FROM posts WHERE id = %s
            ), ins AS (
                INSERT INTO likes (post_id, user_id)
                SELECT post_row.id, liker.id
                FROM post_row CROSS JOIN liker
                ON CONFLICT DO NOTHING
                RETURNING 1
            ), upd AS (
                UPDATE posts
                SET like_count = like_count + 1
                WHERE id = %s AND EXISTS (SELECT 1 FROM ins)
                RETURNING like_count
            )
            SELECT
                EXISTS (SELECT 1 FROM liker) AS user_exists,
                EXISTS (SELECT 1 FROM post_row) AS post_exists,
                EXISTS (SELECT 1 FROM ins) AS inserted,
                COALESCE((SELECT like_count FROM upd), (SELECT like_count FROM posts WHERE id = %s)) AS like_count
            """,
            (username, parsed_post_id, parsed_post_id, parsed_post_id),
        )
        user_exists, post_exists, inserted, like_count = cur.fetchone()

    if not user_exists or not post_exists:
        return json_error("Invalid input")
    status_code = 201 if inserted else 200
    status = "liked" if inserted else "already_liked"
    return jsonify({"status": status, "like_count": like_count}), status_code


@app.get("/feed")
def get_feed():
    username = request.args.get("username")
    if not isinstance(username, str) or username.strip() == "" or len(username.strip()) > 255:
        return json_error("Invalid input")
    username = username.strip()

    page = parse_page()
    if page is None:
        return json_error("Invalid input")
    offset = (page - 1) * PAGE_SIZE

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT p.id, author.username, p.content, p.created_at, p.like_count
            FROM users follower
            JOIN follows f ON f.follower_id = follower.id
            JOIN posts p ON p.user_id = f.following_id
            JOIN users author ON author.id = p.user_id
            WHERE follower.username = %s
            ORDER BY p.created_at DESC, p.id DESC
            LIMIT %s OFFSET %s
            """,
            (username, PAGE_SIZE + 1, offset),
        )
        rows = cur.fetchall()

    items = [feed_post(row) for row in rows[:PAGE_SIZE]]
    return jsonify({"items": items, "page": page, "page_size": PAGE_SIZE, "has_next": len(rows) > PAGE_SIZE})


@app.get("/trending")
def get_trending():
    page = parse_page()
    if page is None:
        return json_error("Invalid input")
    offset = (page - 1) * PAGE_SIZE

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT p.id, author.username, p.content, p.like_count
            FROM posts p
            JOIN users author ON author.id = p.user_id
            ORDER BY p.like_count DESC, p.id DESC
            LIMIT %s OFFSET %s
            """,
            (PAGE_SIZE + 1, offset),
        )
        rows = cur.fetchall()

    items = [trending_post(row) for row in rows[:PAGE_SIZE]]
    return jsonify({"items": items, "page": page, "page_size": PAGE_SIZE, "has_next": len(rows) > PAGE_SIZE})


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


init_db()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False)
