import os
import time
import threading
from flask import Flask, request, jsonify
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", 5432)),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", "postgres"),
    "database": os.environ.get("DB_NAME", "testdb"),
}

_pool_lock = threading.Lock()
_db_pool = None


def get_pool():
    global _db_pool
    if _db_pool is not None:
        return _db_pool
    with _pool_lock:
        if _db_pool is not None:
            return _db_pool
        _db_pool = pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=int(os.environ.get("MICROBLOG_DB_CONNS", "8")),
            **DB_CONFIG,
        )
        return _db_pool


_trending_cache = {"data": None, "expires": 0}
_trending_lock = threading.Lock()

INIT_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(255) UNIQUE NOT NULL,
    full_name VARCHAR(255) NOT NULL,
    bio TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS posts (
    id SERIAL PRIMARY KEY,
    username VARCHAR(255) NOT NULL REFERENCES users(username),
    content TEXT NOT NULL,
    like_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS follows (
    follower_username VARCHAR(255) NOT NULL REFERENCES users(username),
    following_username VARCHAR(255) NOT NULL REFERENCES users(username),
    PRIMARY KEY (follower_username, following_username)
);

CREATE TABLE IF NOT EXISTS likes (
    post_id INTEGER NOT NULL REFERENCES posts(id),
    username VARCHAR(255) NOT NULL REFERENCES users(username),
    PRIMARY KEY (post_id, username)
);

CREATE INDEX IF NOT EXISTS idx_posts_feed_covering
    ON posts(username, created_at DESC)
    INCLUDE (id, content, like_count);
CREATE INDEX IF NOT EXISTS idx_posts_trending_covering
    ON posts(like_count DESC, created_at DESC)
    INCLUDE (id, username, content);
"""

_db_initialized = False
_init_lock = threading.Lock()


def init_db():
    global _db_initialized
    if _db_initialized:
        return
    with _init_lock:
        if _db_initialized:
            return
        p = get_pool()
        conn = p.getconn()
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(INIT_SQL)
            _db_initialized = True
        finally:
            p.putconn(conn)


def get_conn():
    init_db()
    return get_pool().getconn()


def put_conn(conn):
    get_pool().putconn(conn)


@app.route("/users", methods=["POST"])
def create_user():
    data = request.get_json(silent=True)
    if not data or not data.get("username") or not data.get("full_name"):
        return jsonify({"error": "Invalid input"}), 400

    username = data["username"].strip()
    full_name = data["full_name"].strip()
    bio = data.get("bio", "").strip() if data.get("bio") else ""

    if not username or not full_name:
        return jsonify({"error": "Invalid input"}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, full_name, bio) VALUES (%s, %s, %s) ON CONFLICT (username) DO NOTHING RETURNING id",
                (username, full_name, bio),
            )
            row = cur.fetchone()
            conn.commit()
            if row is None:
                return jsonify({"error": "Username already exists"}), 400
        return jsonify({"message": "User created"}), 201
    except Exception:
        conn.rollback()
        return jsonify({"error": "Invalid input or username already exists"}), 400
    finally:
        put_conn(conn)


@app.route("/posts", methods=["POST"])
def create_post():
    data = request.get_json(silent=True)
    if not data or not data.get("username") or not data.get("content"):
        return jsonify({"error": "Invalid input"}), 400

    username = data["username"].strip()
    content = data["content"].strip()

    if not username or not content:
        return jsonify({"error": "Invalid input"}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO posts (username, content) VALUES (%s, %s) RETURNING id",
                (username, content),
            )
            post_id = cur.fetchone()[0]
            conn.commit()
        return jsonify({"message": "Post created", "id": post_id}), 201
    except Exception:
        conn.rollback()
        return jsonify({"error": "Invalid input"}), 400
    finally:
        put_conn(conn)


@app.route("/follow", methods=["POST"])
def follow_user():
    data = request.get_json(silent=True)
    if not data or not data.get("follower_username") or not data.get("following_username"):
        return jsonify({"error": "Invalid input"}), 400

    follower = data["follower_username"].strip()
    following = data["following_username"].strip()

    if not follower or not following or follower == following:
        return jsonify({"error": "Invalid input"}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO follows (follower_username, following_username) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (follower, following),
            )
            conn.commit()
        return jsonify({"message": "Successfully followed"}), 201
    except Exception:
        conn.rollback()
        return jsonify({"error": "Invalid input"}), 400
    finally:
        put_conn(conn)


@app.route("/posts/<int:post_id>/like", methods=["POST"])
def like_post(post_id):
    data = request.get_json(silent=True)
    if not data or not data.get("username"):
        return jsonify({"error": "Invalid input"}), 400

    username = data["username"].strip()
    if not username:
        return jsonify({"error": "Invalid input"}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO likes (post_id, username) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING post_id",
                (post_id, username),
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE posts SET like_count = like_count + 1 WHERE id = %s",
                    (post_id,),
                )
            conn.commit()
        return jsonify({"message": "Liked"}), 201
    except Exception:
        conn.rollback()
        return jsonify({"error": "Invalid input"}), 400
    finally:
        put_conn(conn)


@app.route("/feed", methods=["GET"])
def get_feed():
    username = request.args.get("username", "").strip()
    if not username:
        return jsonify([]), 200

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                WITH visible_users(username) AS (
                    SELECT %s::VARCHAR(255)
                    UNION
                    SELECT f.following_username
                    FROM follows f
                    WHERE f.follower_username = %s
                )
                SELECT p.id, p.username, p.content, p.like_count, p.created_at
                FROM visible_users vu
                JOIN LATERAL (
                    SELECT p.id, p.username, p.content, p.like_count, p.created_at
                    FROM posts p
                    WHERE p.username = vu.username
                    ORDER BY p.created_at DESC
                    LIMIT 50
                ) p ON TRUE
                ORDER BY p.created_at DESC
                LIMIT 50
                """,
                (username, username),
            )
            rows = cur.fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r["id"],
                "username": r["username"],
                "content": r["content"],
                "like_count": r["like_count"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            })
        return jsonify(result), 200
    finally:
        put_conn(conn)


@app.route("/trending", methods=["GET"])
def get_trending():
    now = time.time()
    cached = _trending_cache
    if cached["data"] is not None and now < cached["expires"]:
        return jsonify(cached["data"]), 200

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, username, content, like_count
                FROM posts
                ORDER BY like_count DESC, created_at DESC
                LIMIT 20
                """
            )
            rows = cur.fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r["id"],
                "username": r["username"],
                "content": r["content"],
                "like_count": r["like_count"],
            })
        with _trending_lock:
            _trending_cache["data"] = result
            _trending_cache["expires"] = now + 5
        return jsonify(result), 200
    finally:
        put_conn(conn)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
