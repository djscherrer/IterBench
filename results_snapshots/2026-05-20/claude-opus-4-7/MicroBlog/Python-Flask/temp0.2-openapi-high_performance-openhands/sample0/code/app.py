import os
import threading
import logging
from flask import Flask, request, jsonify
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "postgres")
DB_NAME = os.environ.get("DB_NAME", "testdb")

PAGE_SIZE = 50

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

_pool = None
_pool_lock = threading.Lock()


def get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=20,
                    host=DB_HOST,
                    port=DB_PORT,
                    user=DB_USER,
                    password=DB_PASSWORD,
                    dbname=DB_NAME,
                )
    return _pool


class Conn:
    def __init__(self):
        self.conn = None

    def __enter__(self):
        self.conn = get_pool().getconn()
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is not None:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
        finally:
            get_pool().putconn(self.conn)


def init_db():
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, dbname=DB_NAME
    )
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(728341)")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    full_name TEXT NOT NULL,
                    bio TEXT
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    id BIGSERIAL PRIMARY KEY,
                    username TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    like_count BIGINT NOT NULL DEFAULT 0
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS follows (
                    follower_username TEXT NOT NULL,
                    following_username TEXT NOT NULL,
                    PRIMARY KEY (follower_username, following_username)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS likes (
                    post_id BIGINT NOT NULL,
                    username TEXT NOT NULL,
                    PRIMARY KEY (post_id, username)
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_posts_user_created ON posts (username, created_at DESC, id DESC);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_posts_trending ON posts (like_count DESC, id DESC);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_follows_follower ON follows (follower_username);"
            )
            cur.execute("SELECT pg_advisory_unlock(728341)")
    finally:
        conn.close()


def _bad_request(msg="Invalid input"):
    return jsonify({"error": msg}), 400


@app.route("/users", methods=["POST"])
def create_user():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _bad_request()
    username = data.get("username")
    full_name = data.get("full_name")
    bio = data.get("bio")
    if not isinstance(username, str) or not username or not isinstance(full_name, str) or not full_name:
        return _bad_request()
    if bio is not None and not isinstance(bio, str):
        return _bad_request()
    try:
        with Conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (username, full_name, bio) VALUES (%s, %s, %s) ON CONFLICT (username) DO NOTHING RETURNING id",
                    (username, full_name, bio),
                )
                row = cur.fetchone()
                conn.commit()
                if row is None:
                    return _bad_request("Username already exists")
        return jsonify({"message": "User created"}), 201
    except Exception as e:
        logger.exception("create_user error: %s", e)
        return _bad_request()


@app.route("/posts", methods=["POST"])
def create_post():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _bad_request()
    username = data.get("username")
    content = data.get("content")
    if not isinstance(username, str) or not username or not isinstance(content, str):
        return _bad_request()
    try:
        with Conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM users WHERE username = %s", (username,))
                if cur.fetchone() is None:
                    return _bad_request("User does not exist")
                cur.execute(
                    "INSERT INTO posts (username, content) VALUES (%s, %s) RETURNING id",
                    (username, content),
                )
                pid = cur.fetchone()[0]
                conn.commit()
        return jsonify({"id": pid, "message": "Post created"}), 201
    except Exception as e:
        logger.exception("create_post error: %s", e)
        return _bad_request()


@app.route("/follow", methods=["POST"])
def follow_user():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _bad_request()
    follower = data.get("follower_username")
    following = data.get("following_username")
    if not isinstance(follower, str) or not follower or not isinstance(following, str) or not following:
        return _bad_request()
    if follower == following:
        return _bad_request("Cannot follow self")
    try:
        with Conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT username FROM users WHERE username IN (%s, %s)",
                    (follower, following),
                )
                rows = cur.fetchall()
                names = {r[0] for r in rows}
                if follower not in names or following not in names:
                    return _bad_request("User does not exist")
                cur.execute(
                    "INSERT INTO follows (follower_username, following_username) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (follower, following),
                )
                inserted = cur.rowcount
                conn.commit()
        if inserted == 1:
            return jsonify({"message": "Followed"}), 201
        return jsonify({"message": "Already following"}), 200
    except Exception as e:
        logger.exception("follow error: %s", e)
        return _bad_request()


@app.route("/posts/<int:post_id>/like", methods=["POST"])
def like_post(post_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _bad_request()
    username = data.get("username")
    if not isinstance(username, str) or not username:
        return _bad_request()
    try:
        with Conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM users WHERE username = %s", (username,))
                if cur.fetchone() is None:
                    return _bad_request("User does not exist")
                cur.execute("SELECT 1 FROM posts WHERE id = %s", (post_id,))
                if cur.fetchone() is None:
                    return _bad_request("Post does not exist")
                cur.execute(
                    "INSERT INTO likes (post_id, username) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (post_id, username),
                )
                inserted = cur.rowcount
                if inserted == 1:
                    cur.execute(
                        "UPDATE posts SET like_count = like_count + 1 WHERE id = %s",
                        (post_id,),
                    )
                conn.commit()
        if inserted == 1:
            return jsonify({"message": "Liked"}), 201
        return jsonify({"message": "Already liked"}), 200
    except Exception as e:
        logger.exception("like error: %s", e)
        return _bad_request()


def _parse_page():
    page = request.args.get("page", "1")
    try:
        p = int(page)
        if p < 1:
            p = 1
    except Exception:
        p = 1
    return p


@app.route("/feed", methods=["GET"])
def feed():
    username = request.args.get("username")
    if not username:
        return _bad_request()
    page = _parse_page()
    offset = (page - 1) * PAGE_SIZE
    limit = PAGE_SIZE + 1
    try:
        with Conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT p.id, p.username, p.content, p.created_at, p.like_count
                    FROM posts p
                    JOIN follows f ON f.following_username = p.username
                    WHERE f.follower_username = %s
                    ORDER BY p.created_at DESC, p.id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (username, limit, offset),
                )
                rows = cur.fetchall()
        has_next = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]
        items = [
            {
                "id": r["id"],
                "username": r["username"],
                "content": r["content"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "like_count": r["like_count"],
            }
            for r in rows
        ]
        return jsonify({
            "items": items,
            "page": page,
            "page_size": PAGE_SIZE,
            "has_next": has_next,
        }), 200
    except Exception as e:
        logger.exception("feed error: %s", e)
        return _bad_request()


@app.route("/trending", methods=["GET"])
def trending():
    page = _parse_page()
    offset = (page - 1) * PAGE_SIZE
    limit = PAGE_SIZE + 1
    try:
        with Conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, username, content, like_count
                    FROM posts
                    ORDER BY like_count DESC, id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (limit, offset),
                )
                rows = cur.fetchall()
        has_next = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]
        items = [
            {
                "id": r["id"],
                "username": r["username"],
                "content": r["content"],
                "like_count": r["like_count"],
            }
            for r in rows
        ]
        return jsonify({
            "items": items,
            "page": page,
            "page_size": PAGE_SIZE,
            "has_next": has_next,
        }), 200
    except Exception as e:
        logger.exception("trending error: %s", e)
        return _bad_request()


# Initialize DB at import (gunicorn --preload triggers this once in master before forking)
try:
    init_db()
except Exception as e:
    logger.exception("init_db failed: %s", e)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port)
