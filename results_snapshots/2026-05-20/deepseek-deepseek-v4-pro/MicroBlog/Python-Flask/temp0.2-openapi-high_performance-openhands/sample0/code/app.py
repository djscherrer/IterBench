import os
import logging
import threading
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from psycopg2.pool import ThreadedConnectionPool
import psycopg2.extras

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

PAGE_SIZE = 50

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", "postgres"),
    "dbname": os.environ.get("DB_NAME", "testdb"),
}

# Per-worker connection pool management (safe for gunicorn --preload with multiple workers)
_pools = {}
_pools_lock = threading.Lock()
_db_initialized = set()
_db_init_lock = threading.Lock()


def get_pool():
    """Get or create a connection pool for the current worker process."""
    pid = os.getpid()
    if pid not in _pools:
        with _pools_lock:
            if pid not in _pools:
                _pools[pid] = ThreadedConnectionPool(
                    minconn=1,
                    maxconn=10,
                    **DB_CONFIG,
                )
    return _pools[pid]


def get_conn():
    return get_pool().getconn()


def put_conn(conn):
    get_pool().putconn(conn)


def init_db():
    """Initialize database tables and indexes. Safe for concurrent workers."""
    pid = os.getpid()
    if pid in _db_initialized:
        return
    with _db_init_lock:
        if pid in _db_initialized:
            return
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        username VARCHAR(255) UNIQUE NOT NULL,
                        full_name VARCHAR(255) NOT NULL,
                        bio TEXT DEFAULT '',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );

                    CREATE TABLE IF NOT EXISTS posts (
                        id SERIAL PRIMARY KEY,
                        username VARCHAR(255) NOT NULL REFERENCES users(username),
                        content TEXT NOT NULL,
                        like_count INTEGER NOT NULL DEFAULT 0,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );

                    CREATE TABLE IF NOT EXISTS follows (
                        follower_username VARCHAR(255) NOT NULL REFERENCES users(username),
                        following_username VARCHAR(255) NOT NULL REFERENCES users(username),
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (follower_username, following_username)
                    );

                    CREATE TABLE IF NOT EXISTS likes (
                        username VARCHAR(255) NOT NULL REFERENCES users(username),
                        post_id INTEGER NOT NULL REFERENCES posts(id),
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (username, post_id)
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_follows_follower
                        ON follows (follower_username);
                    CREATE INDEX IF NOT EXISTS idx_posts_username_created
                        ON posts (username, created_at DESC, id DESC);
                    CREATE INDEX IF NOT EXISTS idx_posts_likes
                        ON posts (like_count DESC, id DESC);
                    CREATE INDEX IF NOT EXISTS idx_posts_created_id
                        ON posts (created_at DESC, id DESC);
                    """
                )
            conn.commit()
        finally:
            put_conn(conn)
        _db_initialized.add(pid)


@app.before_request
def ensure_db():
    init_db()


# ---------------------------------------------------------------------------
# POST /users
# ---------------------------------------------------------------------------
@app.route("/users", methods=["POST"])
def create_user():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    username = (data.get("username") or "").strip()
    full_name = (data.get("full_name") or "").strip()

    if not username or not full_name:
        return jsonify({"error": "username and full_name are required"}), 400

    bio = (data.get("bio") or "").strip()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, full_name, bio) VALUES (%s, %s, %s)",
                (username, full_name, bio),
            )
        conn.commit()
        return jsonify({"message": "User created"}), 201
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({"error": "Username already exists"}), 400
    except Exception as e:
        conn.rollback()
        logger.error("Error creating user: %s", e)
        return jsonify({"error": "Internal server error"}), 500
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# POST /posts
# ---------------------------------------------------------------------------
@app.route("/posts", methods=["POST"])
def create_post():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    username = (data.get("username") or "").strip()
    content = (data.get("content") or "").strip()

    if not username or not content:
        return jsonify({"error": "username and content are required"}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Verify user exists
            cur.execute("SELECT 1 FROM users WHERE username = %s", (username,))
            if cur.fetchone() is None:
                return jsonify({"error": "User not found"}), 400

            now = datetime.now(timezone.utc)
            cur.execute(
                "INSERT INTO posts (username, content, created_at) VALUES (%s, %s, %s) RETURNING id",
                (username, content, now),
            )
            post_id = cur.fetchone()[0]
        conn.commit()
        return (
            jsonify(
                {
                    "id": post_id,
                    "username": username,
                    "content": content,
                    "created_at": now.isoformat(),
                    "like_count": 0,
                }
            ),
            201,
        )
    except Exception as e:
        conn.rollback()
        logger.error("Error creating post: %s", e)
        return jsonify({"error": "Internal server error"}), 500
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# POST /follow
# ---------------------------------------------------------------------------
@app.route("/follow", methods=["POST"])
def follow_user():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    follower = (data.get("follower_username") or "").strip()
    following = (data.get("following_username") or "").strip()

    if not follower or not following:
        return jsonify({"error": "follower_username and following_username are required"}), 400

    if follower == following:
        return jsonify({"error": "Cannot follow yourself"}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Check both users exist
            cur.execute("SELECT 1 FROM users WHERE username IN (%s, %s)", (follower, following))
            rows = cur.fetchall()
            if len(rows) != 2:
                return jsonify({"error": "One or both users not found"}), 400

            cur.execute(
                "INSERT INTO follows (follower_username, following_username) VALUES (%s, %s) "
                "ON CONFLICT (follower_username, following_username) DO NOTHING "
                "RETURNING created_at",
                (follower, following),
            )
            result = cur.fetchone()
        conn.commit()
        if result is not None:
            return jsonify({"message": "Successfully followed"}), 201
        else:
            return jsonify({"message": "Already following"}), 200
    except Exception as e:
        conn.rollback()
        logger.error("Error following user: %s", e)
        return jsonify({"error": "Internal server error"}), 500
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# POST /posts/{postId}/like
# ---------------------------------------------------------------------------
@app.route("/posts/<int:post_id>/like", methods=["POST"])
def like_post(post_id):
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    username = (data.get("username") or "").strip()
    if not username:
        return jsonify({"error": "username is required"}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Verify user exists
            cur.execute("SELECT 1 FROM users WHERE username = %s", (username,))
            if cur.fetchone() is None:
                return jsonify({"error": "User not found"}), 400

            # Verify post exists
            cur.execute("SELECT 1 FROM posts WHERE id = %s", (post_id,))
            if cur.fetchone() is None:
                return jsonify({"error": "Post not found"}), 400

            # Try to insert like
            cur.execute(
                "INSERT INTO likes (username, post_id) VALUES (%s, %s) "
                "ON CONFLICT (username, post_id) DO NOTHING "
                "RETURNING created_at",
                (username, post_id),
            )
            result = cur.fetchone()

            if result is not None:
                # New like - increment counter
                cur.execute(
                    "UPDATE posts SET like_count = like_count + 1 WHERE id = %s",
                    (post_id,),
                )
                conn.commit()
                return jsonify({"message": "Liked"}), 201
            else:
                conn.commit()
                return jsonify({"message": "Already liked"}), 200
    except Exception as e:
        conn.rollback()
        logger.error("Error liking post: %s", e)
        return jsonify({"error": "Internal server error"}), 500
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# GET /feed
# ---------------------------------------------------------------------------
@app.route("/feed", methods=["GET"])
def get_feed():
    username = (request.args.get("username") or "").strip()
    if not username:
        return jsonify({"error": "username query parameter is required"}), 400

    try:
        page = int(request.args.get("page", 1))
    except (ValueError, TypeError):
        page = 1
    if page < 1:
        page = 1

    offset = (page - 1) * PAGE_SIZE
    limit = PAGE_SIZE + 1  # fetch one extra to determine has_next

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Verify user exists
            cur.execute("SELECT 1 FROM users WHERE username = %s", (username,))
            if cur.fetchone() is None:
                return jsonify({"error": "User not found"}), 400

            cur.execute(
                """
                SELECT p.id, p.username, p.content, p.like_count, p.created_at
                FROM posts p
                JOIN follows f ON p.username = f.following_username
                WHERE f.follower_username = %s
                ORDER BY p.created_at DESC, p.id DESC
                LIMIT %s OFFSET %s
                """,
                (username, limit, offset),
            )
            rows = cur.fetchall()
    finally:
        put_conn(conn)

    has_next = len(rows) > PAGE_SIZE
    items = rows[:PAGE_SIZE]

    result = []
    for row in items:
        result.append(
            {
                "id": row["id"],
                "username": row["username"],
                "content": row["content"],
                "like_count": row["like_count"],
                "created_at": row["created_at"].isoformat(),
            }
        )

    return (
        jsonify(
            {
                "items": result,
                "page": page,
                "page_size": PAGE_SIZE,
                "has_next": has_next,
            }
        ),
        200,
    )


# ---------------------------------------------------------------------------
# GET /trending
# ---------------------------------------------------------------------------
@app.route("/trending", methods=["GET"])
def get_trending():
    try:
        page = int(request.args.get("page", 1))
    except (ValueError, TypeError):
        page = 1
    if page < 1:
        page = 1

    offset = (page - 1) * PAGE_SIZE
    limit = PAGE_SIZE + 1

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT p.id, p.username, p.content, p.like_count
                FROM posts p
                ORDER BY p.like_count DESC, p.id DESC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            rows = cur.fetchall()
    finally:
        put_conn(conn)

    has_next = len(rows) > PAGE_SIZE
    items = rows[:PAGE_SIZE]

    result = []
    for row in items:
        result.append(
            {
                "id": row["id"],
                "username": row["username"],
                "content": row["content"],
                "like_count": row["like_count"],
            }
        )

    return (
        jsonify(
            {
                "items": result,
                "page": page,
                "page_size": PAGE_SIZE,
                "has_next": has_next,
            }
        ),
        200,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
