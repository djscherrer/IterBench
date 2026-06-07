import os
import threading
from flask import Flask, request, jsonify, g
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

app = Flask(__name__)

DB_CONFIG = {
    'host': os.environ.get('DB_HOST', 'localhost'),
    'port': int(os.environ.get('DB_PORT', 5432)),
    'user': os.environ.get('DB_USER', 'postgres'),
    'password': os.environ.get('DB_PASSWORD', 'postgres'),
    'dbname': os.environ.get('DB_NAME', 'testdb'),
}

PAGE_SIZE = 50

_pools = {}
_pool_lock = threading.Lock()
_init_lock = threading.Lock()
_initialized_pids = set()


def _get_pool():
    pid = os.getpid()
    if pid not in _pools:
        with _pool_lock:
            if pid not in _pools:
                _pools[pid] = ThreadedConnectionPool(1, 10, **DB_CONFIG)
    return _pools[pid]


def _get_conn():
    pool = _get_pool()
    conn = pool.getconn()
    if os.getpid() not in _initialized_pids:
        with _init_lock:
            if os.getpid() not in _initialized_pids:
                _init_schema(conn)
                _initialized_pids.add(os.getpid())
    return conn


def _put_conn(conn):
    try:
        conn.rollback()
    except Exception:
        pass
    pool = _get_pool()
    pool.putconn(conn)


def _init_schema(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(255) UNIQUE NOT NULL,
                full_name VARCHAR(255) NOT NULL,
                bio TEXT NOT NULL DEFAULT '',
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
                PRIMARY KEY (follower_username, following_username)
            );

            CREATE TABLE IF NOT EXISTS likes (
                username VARCHAR(255) NOT NULL REFERENCES users(username),
                post_id INTEGER NOT NULL REFERENCES posts(id),
                PRIMARY KEY (username, post_id)
            );
        """)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_posts_username_created
                ON posts(username, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_posts_like_count_id
                ON posts(like_count DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_follows_follower
                ON follows(follower_username);
        """)
    conn.commit()


@app.before_request
def _before_request():
    g.db_conn = _get_conn()


@app.teardown_request
def _teardown_request(_exception):
    conn = g.pop('db_conn', None)
    if conn is not None:
        _put_conn(conn)


# ---------------------------------------------------------------------------
# POST /users
# ---------------------------------------------------------------------------
@app.route('/users', methods=['POST'])
def create_user():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid input'}), 400

    username = data.get('username')
    full_name = data.get('full_name')
    if not username or not full_name:
        return jsonify({'error': 'Invalid input'}), 400

    bio = data.get('bio', '')

    try:
        with g.db_conn.cursor() as cur:
            cur.execute(
                'INSERT INTO users (username, full_name, bio) VALUES (%s, %s, %s)',
                (username, full_name, bio)
            )
        g.db_conn.commit()
    except psycopg2.Error:
        g.db_conn.rollback()
        return jsonify({'error': 'Invalid input or username already exists'}), 400

    return '', 201


# ---------------------------------------------------------------------------
# POST /posts
# ---------------------------------------------------------------------------
@app.route('/posts', methods=['POST'])
def create_post():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid input'}), 400

    username = data.get('username')
    content = data.get('content')
    if not username or not content:
        return jsonify({'error': 'Invalid input'}), 400

    try:
        with g.db_conn.cursor() as cur:
            cur.execute(
                'INSERT INTO posts (username, content) VALUES (%s, %s) RETURNING id',
                (username, content)
            )
            post_id = cur.fetchone()[0]
        g.db_conn.commit()
    except psycopg2.Error:
        g.db_conn.rollback()
        return jsonify({'error': 'Invalid input'}), 400

    return jsonify({'id': post_id}), 201


# ---------------------------------------------------------------------------
# POST /follow
# ---------------------------------------------------------------------------
@app.route('/follow', methods=['POST'])
def follow_user():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid input'}), 400

    follower = data.get('follower_username')
    following = data.get('following_username')
    if not follower or not following:
        return jsonify({'error': 'Invalid input'}), 400

    try:
        with g.db_conn.cursor() as cur:
            cur.execute(
                'INSERT INTO follows (follower_username, following_username) VALUES (%s, %s)',
                (follower, following)
            )
        g.db_conn.commit()
    except psycopg2.Error:
        g.db_conn.rollback()
        return jsonify({'error': 'Invalid input'}), 400

    return '', 201


# ---------------------------------------------------------------------------
# POST /posts/<postId>/like
# ---------------------------------------------------------------------------
@app.route('/posts/<int:post_id>/like', methods=['POST'])
def like_post(post_id):
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid input'}), 400

    username = data.get('username')
    if not username:
        return jsonify({'error': 'Invalid input'}), 400

    try:
        with g.db_conn.cursor() as cur:
            cur.execute(
                'INSERT INTO likes (username, post_id) VALUES (%s, %s)',
                (username, post_id)
            )
            cur.execute(
                'UPDATE posts SET like_count = like_count + 1 WHERE id = %s',
                (post_id,)
            )
        g.db_conn.commit()
    except psycopg2.Error:
        g.db_conn.rollback()
        return jsonify({'error': 'Invalid input'}), 400

    return '', 201


# ---------------------------------------------------------------------------
# GET /feed
# ---------------------------------------------------------------------------
@app.route('/feed', methods=['GET'])
def get_feed():
    username = request.args.get('username')
    if not username:
        return jsonify({'error': 'username is required'}), 400

    try:
        page = int(request.args.get('page', 1))
    except (ValueError, TypeError):
        page = 1
    if page < 1:
        page = 1

    offset = (page - 1) * PAGE_SIZE
    limit = PAGE_SIZE + 1  # fetch one extra to determine has_next

    with g.db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            '''
            SELECT p.id, p.username, p.content, p.created_at, p.like_count
            FROM posts p
            JOIN follows f ON p.username = f.following_username
            WHERE f.follower_username = %s
            ORDER BY p.created_at DESC
            LIMIT %s OFFSET %s
            ''',
            (username, limit, offset)
        )
        rows = cur.fetchall()

    has_next = len(rows) > PAGE_SIZE
    items = rows[:PAGE_SIZE]

    for item in items:
        item['created_at'] = item['created_at'].isoformat()

    return jsonify({
        'items': items,
        'page': page,
        'page_size': PAGE_SIZE,
        'has_next': has_next,
    }), 200


# ---------------------------------------------------------------------------
# GET /trending
# ---------------------------------------------------------------------------
@app.route('/trending', methods=['GET'])
def get_trending():
    try:
        page = int(request.args.get('page', 1))
    except (ValueError, TypeError):
        page = 1
    if page < 1:
        page = 1

    offset = (page - 1) * PAGE_SIZE
    limit = PAGE_SIZE + 1

    with g.db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            '''
            SELECT id, username, content, like_count
            FROM posts
            ORDER BY like_count DESC, id DESC
            LIMIT %s OFFSET %s
            ''',
            (limit, offset)
        )
        rows = cur.fetchall()

    has_next = len(rows) > PAGE_SIZE
    items = rows[:PAGE_SIZE]

    return jsonify({
        'items': items,
        'page': page,
        'page_size': PAGE_SIZE,
        'has_next': has_next,
    }), 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)
