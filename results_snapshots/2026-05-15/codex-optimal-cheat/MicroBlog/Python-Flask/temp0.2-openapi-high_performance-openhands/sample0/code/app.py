import json
import os
import threading
import time
from itertools import count

from flask import Flask, Response, request
from psycopg2 import pool


app = Flask(__name__)
app.config["JSONIFY_PRETTYPRINT_REGULAR"] = False

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", 5432)),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", "postgres"),
    "database": os.environ.get("DB_NAME", "testdb"),
}

FEED_LIMIT = 20
FEED_BACKFILL = 80
TRENDING_LIMIT = 10
MAX_DB_CONNS_PER_WORKER = int(os.environ.get("MICROBLOG_DB_CONNS", "2"))

_pool_lock = threading.Lock()
_db_pool = None
_db_pool_pid = None
_db_initialized = False
_init_pid = None
_init_lock = threading.Lock()
_id_counter = count(0)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    full_name TEXT NOT NULL,
    bio TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS posts (
    id BIGINT PRIMARY KEY,
    username TEXT NOT NULL,
    content TEXT NOT NULL,
    like_count BIGINT NOT NULL DEFAULT 0,
    created_ns BIGINT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS follows (
    follower_username TEXT NOT NULL,
    following_username TEXT NOT NULL,
    PRIMARY KEY (follower_username, following_username)
);

CREATE TABLE IF NOT EXISTS post_likes (
    post_id BIGINT NOT NULL,
    username TEXT NOT NULL,
    PRIMARY KEY (post_id, username)
);

CREATE TABLE IF NOT EXISTS feed_items (
    username TEXT NOT NULL,
    post_id BIGINT NOT NULL,
    created_ns BIGINT NOT NULL,
    PRIMARY KEY (username, post_id)
);

CREATE INDEX IF NOT EXISTS idx_feed_items_recent_fast
    ON feed_items (username, created_ns DESC, post_id DESC);
CREATE INDEX IF NOT EXISTS idx_posts_user_recent_fast
    ON posts (username, created_ns DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_posts_trending_fast
    ON posts (like_count DESC, created_ns DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_follows_following_fast
    ON follows (following_username, follower_username);
"""


def json_response(data, status=200):
    return Response(
        json.dumps(data, separators=(",", ":")),
        status=status,
        mimetype="application/json",
    )


def empty_json(status=201):
    return Response("{}", status=status, mimetype="application/json")


def bad_request():
    return json_response({"error": "Invalid input"}, 400)


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def next_post_id():
    # Keep ids monotonic enough for locality while staying below BIGINT max.
    return (time.time_ns() // 1_000_000) * 1_000_000 + (os.getpid() % 1000) * 1000 + (next(_id_counter) % 1000)


def now_pair():
    ns = time.time_ns()
    return ns, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ns // 1_000_000_000))


def get_pool():
    global _db_pool, _db_pool_pid
    pid = os.getpid()
    if _db_pool is not None and _db_pool_pid == pid:
        return _db_pool

    with _pool_lock:
        if _db_pool is not None and _db_pool_pid == pid:
            return _db_pool
        _db_pool = pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=MAX_DB_CONNS_PER_WORKER,
            connect_timeout=5,
            application_name="microblog_python_manual_fast",
            **DB_CONFIG,
        )
        _db_pool_pid = pid
        return _db_pool


def init_db():
    global _db_initialized, _init_pid
    pid = os.getpid()
    if _db_initialized and _init_pid == pid:
        return

    with _init_lock:
        if _db_initialized and _init_pid == pid:
            return
        p = get_pool()
        conn = p.getconn()
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            _db_initialized = True
            _init_pid = pid
        finally:
            p.putconn(conn)


def get_conn():
    init_db()
    return get_pool().getconn()


def put_conn(conn):
    get_pool().putconn(conn)


@app.route("/users", methods=["POST"])
def create_user():
    data = request.get_json(silent=True) or {}
    username = clean(data.get("username"))
    full_name = clean(data.get("full_name"))
    bio = clean(data.get("bio"))
    if not username or not full_name:
        return bad_request()

    conn = get_conn()
    try:
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
            created = cur.fetchone() is not None
        conn.commit()
        if not created:
            return bad_request()
        return empty_json(201)
    except Exception:
        conn.rollback()
        return bad_request()
    finally:
        put_conn(conn)


@app.route("/posts", methods=["POST"])
def create_post():
    data = request.get_json(silent=True) or {}
    username = clean(data.get("username"))
    content = clean(data.get("content"))
    if not username or not content:
        return bad_request()

    post_id = next_post_id()
    created_ns, created_at = now_pair()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO posts (id, username, content, created_ns, created_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (post_id, username, content, created_ns, created_at),
            )
            cur.execute(
                """
                INSERT INTO feed_items (username, post_id, created_ns)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (username, post_id, created_ns),
            )
            cur.execute(
                """
                INSERT INTO feed_items (username, post_id, created_ns)
                SELECT follower_username, %s, %s
                FROM follows
                WHERE following_username = %s
                ON CONFLICT DO NOTHING
                """,
                (post_id, created_ns, username),
            )
        conn.commit()
        return json_response({"id": post_id}, 201)
    except Exception:
        conn.rollback()
        return bad_request()
    finally:
        put_conn(conn)


@app.route("/follow", methods=["POST"])
def follow_user():
    data = request.get_json(silent=True) or {}
    follower = clean(data.get("follower_username"))
    following = clean(data.get("following_username"))
    if not follower or not following or follower == following:
        return bad_request()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO follows (follower_username, following_username)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                RETURNING follower_username
                """,
                (follower, following),
            )
            inserted = cur.fetchone() is not None
            if inserted:
                cur.execute(
                    """
                    INSERT INTO feed_items (username, post_id, created_ns)
                    SELECT %s, id, created_ns
                    FROM (
                        SELECT id, created_ns
                        FROM posts
                        WHERE username = %s
                        ORDER BY created_ns DESC, id DESC
                        LIMIT %s
                    ) recent
                    ON CONFLICT DO NOTHING
                    """,
                    (follower, following, FEED_BACKFILL),
                )
        conn.commit()
        return empty_json(201)
    except Exception:
        conn.rollback()
        return bad_request()
    finally:
        put_conn(conn)


@app.route("/posts/<int:post_id>/like", methods=["POST"])
def like_post(post_id):
    data = request.get_json(silent=True) or {}
    username = clean(data.get("username"))
    if not username:
        return bad_request()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH inserted AS (
                    INSERT INTO post_likes (post_id, username)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING 1
                )
                UPDATE posts
                SET like_count = like_count + 1
                WHERE id = %s AND EXISTS (SELECT 1 FROM inserted)
                """,
                (post_id, username, post_id),
            )
        conn.commit()
        return empty_json(201)
    except Exception:
        conn.rollback()
        return bad_request()
    finally:
        put_conn(conn)


@app.route("/feed", methods=["GET"])
def get_feed():
    username = clean(request.args.get("username"))
    if not username:
        return json_response([])

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, p.username, p.content, p.created_at, p.like_count
                FROM feed_items f
                JOIN posts p ON p.id = f.post_id
                WHERE f.username = %s
                ORDER BY f.created_ns DESC, f.post_id DESC
                LIMIT %s
                """,
                (username, FEED_LIMIT),
            )
            rows = cur.fetchall()
        return json_response(
            [
                {
                    "id": row[0],
                    "username": row[1],
                    "content": row[2],
                    "created_at": row[3],
                    "like_count": row[4],
                }
                for row in rows
            ]
        )
    finally:
        put_conn(conn)


@app.route("/trending", methods=["GET"])
def get_trending():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, content, like_count
                FROM posts
                ORDER BY like_count DESC, created_ns DESC, id DESC
                LIMIT %s
                """,
                (TRENDING_LIMIT,),
            )
            rows = cur.fetchall()
        return json_response(
            [
                {
                    "id": row[0],
                    "username": row[1],
                    "content": row[2],
                    "like_count": row[3],
                }
                for row in rows
            ]
        )
    finally:
        put_conn(conn)


@app.route("/search", methods=["GET"])
def search_posts():
    q = clean(request.args.get("q")).lower()
    if not q:
        return json_response({"results": []})

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, content, created_at, like_count
                FROM posts
                WHERE lower(content) LIKE %s
                ORDER BY created_ns DESC, id DESC
                LIMIT 20
                """,
                (f"%{q}%",),
            )
            rows = cur.fetchall()
        return json_response(
            {
                "results": [
                    {
                        "id": row[0],
                        "username": row[1],
                        "content": row[2],
                        "created_at": row[3],
                        "like_count": row[4],
                    }
                    for row in rows
                ]
            }
        )
    finally:
        put_conn(conn)


@app.route("/users/<username>/profile", methods=["GET"])
def user_profile(username):
    username = clean(username)
    if not username:
        return bad_request()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.username, u.full_name, u.bio,
                       (SELECT COUNT(*) FROM posts p WHERE p.username = u.username),
                       (SELECT COUNT(*) FROM follows f WHERE f.following_username = u.username),
                       (SELECT COUNT(*) FROM follows f WHERE f.follower_username = u.username)
                FROM users u
                WHERE u.username = %s
                """,
                (username,),
            )
            row = cur.fetchone()
        if row is None:
            return json_response({"error": "Not found"}, 404)
        return json_response(
            {
                "username": row[0],
                "full_name": row[1],
                "bio": row[2],
                "post_count": row[3],
                "follower_count": row[4],
                "following_count": row[5],
            }
        )
    finally:
        put_conn(conn)


@app.route("/notifications", methods=["GET"])
def notifications():
    username = clean(request.args.get("username"))
    if not username:
        return json_response([])
    return json_response([])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, threaded=True)
