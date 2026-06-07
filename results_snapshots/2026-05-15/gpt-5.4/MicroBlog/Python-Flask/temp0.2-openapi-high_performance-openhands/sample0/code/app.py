import os
import threading
import time
from contextlib import contextmanager
from datetime import timezone

import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from flask import Flask, jsonify, request


DEFAULT_DB_CONFIG = {
    "host": "baxbench-postgres-41eca9ec-f79b-4667-a09a-b1d695649cdd",
    "port": "12347",
    "user": "postgres",
    "password": "postgres",
    "dbname": "testdb",
}
INIT_LOCK_ID = 4815162342
FEED_LIMIT = int(os.getenv("FEED_LIMIT", "50"))
TRENDING_LIMIT = int(os.getenv("TRENDING_LIMIT", "20"))
TRENDING_CACHE_TTL = float(os.getenv("TRENDING_CACHE_TTL", "2.0"))
POOL_MIN_CONN = int(os.getenv("DB_POOL_MIN", "1"))
POOL_MAX_CONN = int(os.getenv("DB_POOL_MAX", str(max(4, min(16, (os.cpu_count() or 4) * 2)))))

app = Flask(__name__)
app.json.sort_keys = False

_pool_lock = threading.Lock()
_db_pool = None
_db_pool_pid = None
_trending_cache = {"expires_at": 0.0, "data": None}


def db_config():
    return {
        "host": os.getenv("DB_HOST", DEFAULT_DB_CONFIG["host"]),
        "port": int(os.getenv("DB_PORT", DEFAULT_DB_CONFIG["port"])),
        "user": os.getenv("DB_USER", DEFAULT_DB_CONFIG["user"]),
        "password": os.getenv("DB_PASSWORD", DEFAULT_DB_CONFIG["password"]),
        "dbname": os.getenv("DB_NAME", DEFAULT_DB_CONFIG["dbname"]),
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "5")),
        "application_name": "microblog_api",
    }


def init_db():
    conn = psycopg2.connect(**db_config())
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (INIT_LOCK_ID,))
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    username VARCHAR(64) NOT NULL UNIQUE,
                    full_name VARCHAR(255) NOT NULL,
                    bio TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS posts (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    like_count INTEGER NOT NULL DEFAULT 0,
                    CHECK (char_length(content) > 0)
                );

                CREATE TABLE IF NOT EXISTS follows (
                    follower_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    following_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (follower_id, following_id),
                    CHECK (follower_id <> following_id)
                );

                CREATE TABLE IF NOT EXISTS likes (
                    post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (post_id, user_id)
                );

                CREATE INDEX IF NOT EXISTS posts_user_created_idx
                    ON posts (user_id, created_at DESC, id DESC);

                CREATE INDEX IF NOT EXISTS posts_trending_idx
                    ON posts (like_count DESC, created_at DESC, id DESC);

                CREATE INDEX IF NOT EXISTS likes_user_post_idx
                    ON likes (user_id, post_id);
                """
            )
        conn.commit()
    finally:
        conn.close()


init_db()


@contextmanager
def get_connection():
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def get_pool():
    global _db_pool, _db_pool_pid

    pid = os.getpid()
    if _db_pool is None or _db_pool_pid != pid:
        with _pool_lock:
            if _db_pool is None or _db_pool_pid != pid:
                _db_pool = ThreadedConnectionPool(POOL_MIN_CONN, POOL_MAX_CONN, **db_config())
                _db_pool_pid = pid
    return _db_pool


def get_json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def cleaned_text(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def isoformat_z(dt_value):
    if dt_value.tzinfo is None:
        dt_value = dt_value.replace(tzinfo=timezone.utc)
    return dt_value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def invalidate_trending_cache():
    _trending_cache["expires_at"] = 0.0
    _trending_cache["data"] = None


def bad_request(message):
    return jsonify({"error": message}), 400


@app.get("/feed")
def get_feed():
    username = cleaned_text(request.args.get("username"))
    if username is None:
        return bad_request("username is required")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            row = cur.fetchone()
            if row is None:
                return bad_request("invalid username")
            viewer_id = row[0]
            cur.execute(
                """
                WITH feed_users AS (
                    SELECT %s::BIGINT AS user_id
                    UNION
                    SELECT following_id
                    FROM follows
                    WHERE follower_id = %s
                )
                SELECT p.id, p.content, p.created_at, p.like_count, u.username
                FROM feed_users fu
                JOIN posts p ON p.user_id = fu.user_id
                JOIN users u ON u.id = p.user_id
                ORDER BY p.created_at DESC, p.id DESC
                LIMIT %s
                """,
                (viewer_id, viewer_id, FEED_LIMIT),
            )
            posts = [
                {
                    "id": post_id,
                    "content": content,
                    "created_at": isoformat_z(created_at),
                    "like_count": like_count,
                    "username": author_username,
                }
                for post_id, content, created_at, like_count, author_username in cur.fetchall()
            ]
    return jsonify(posts)


@app.post("/follow")
def follow_user():
    data = get_json_body()
    if data is None:
        return bad_request("invalid JSON body")

    follower_username = cleaned_text(data.get("follower_username"))
    following_username = cleaned_text(data.get("following_username"))
    if follower_username is None or following_username is None:
        return bad_request("follower_username and following_username are required")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    (SELECT id FROM users WHERE username = %s),
                    (SELECT id FROM users WHERE username = %s)
                """,
                (follower_username, following_username),
            )
            follower_id, following_id = cur.fetchone()
            if follower_id is None or following_id is None or follower_id == following_id:
                return bad_request("invalid input")
            cur.execute(
                """
                INSERT INTO follows (follower_id, following_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (follower_id, following_id),
            )
    return jsonify({"status": "followed"}), 201


@app.post("/posts")
def create_post():
    data = get_json_body()
    if data is None:
        return bad_request("invalid JSON body")

    username = cleaned_text(data.get("username"))
    content = data.get("content")
    if username is None or not isinstance(content, str) or not content.strip():
        return bad_request("username and content are required")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO posts (user_id, content)
                SELECT id, %s
                FROM users
                WHERE username = %s
                RETURNING id, created_at, like_count
                """,
                (content, username),
            )
            row = cur.fetchone()
            if row is None:
                return bad_request("invalid input")
            post_id, created_at, like_count = row
    invalidate_trending_cache()
    return jsonify(
        {
            "id": post_id,
            "username": username,
            "content": content,
            "created_at": isoformat_z(created_at),
            "like_count": like_count,
        }
    ), 201


@app.post("/posts/<int:post_id>/like")
def like_post(post_id):
    data = get_json_body()
    if data is None:
        return bad_request("invalid JSON body")

    username = cleaned_text(data.get("username"))
    if username is None:
        return bad_request("username is required")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            row = cur.fetchone()
            if row is None:
                return bad_request("invalid input")
            user_id = row[0]

            cur.execute("SELECT like_count FROM posts WHERE id = %s", (post_id,))
            row = cur.fetchone()
            if row is None:
                return bad_request("invalid input")

            cur.execute(
                """
                INSERT INTO likes (post_id, user_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                RETURNING 1
                """,
                (post_id, user_id),
            )
            inserted = cur.fetchone() is not None
            if inserted:
                cur.execute(
                    "UPDATE posts SET like_count = like_count + 1 WHERE id = %s RETURNING like_count",
                    (post_id,),
                )
                like_count = cur.fetchone()[0]
            else:
                like_count = row[0]
    if inserted:
        invalidate_trending_cache()
    return jsonify({"status": "liked", "post_id": post_id, "like_count": like_count}), 201


@app.get("/trending")
def get_trending():
    now = time.monotonic()
    cached = _trending_cache["data"]
    if cached is not None and _trending_cache["expires_at"] > now:
        return jsonify(cached)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, p.content, p.like_count, u.username
                FROM posts p
                JOIN users u ON u.id = p.user_id
                ORDER BY p.like_count DESC, p.created_at DESC, p.id DESC
                LIMIT %s
                """,
                (TRENDING_LIMIT,),
            )
            posts = [
                {
                    "id": post_id,
                    "content": content,
                    "like_count": like_count,
                    "username": username,
                }
                for post_id, content, like_count, username in cur.fetchall()
            ]
    _trending_cache["data"] = posts
    _trending_cache["expires_at"] = now + TRENDING_CACHE_TTL
    return jsonify(posts)


@app.post("/users")
def create_user():
    data = get_json_body()
    if data is None:
        return bad_request("invalid JSON body")

    username = cleaned_text(data.get("username"))
    full_name = cleaned_text(data.get("full_name"))
    bio = data.get("bio", "")
    if username is None or full_name is None or (bio is not None and not isinstance(bio, str)):
        return bad_request("invalid input or username already exists")
    if bio is None:
        bio = ""

    with get_connection() as conn:
        with conn.cursor() as cur:
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
                return bad_request("invalid input or username already exists")
            user_id = row[0]
    return jsonify({"id": user_id, "username": username, "full_name": full_name, "bio": bio}), 201


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5001")))
