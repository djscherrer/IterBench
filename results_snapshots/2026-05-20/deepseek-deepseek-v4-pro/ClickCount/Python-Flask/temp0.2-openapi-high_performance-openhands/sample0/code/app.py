import os
import threading
from datetime import date

from flask import Flask, request, jsonify
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Database configuration from environment
# ---------------------------------------------------------------------------
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "postgres")
DB_NAME = os.environ.get("DB_NAME", "testdb")

# ---------------------------------------------------------------------------
# Fork-safe, thread-safe lazy connection pool
# ---------------------------------------------------------------------------
_pool = None
_pool_pid = None
_pool_lock = threading.Lock()

POOL_MIN = 2
POOL_MAX = 15
MAX_CLICKS = 10000

VALID_DIRECTIONS = frozenset({"<", ">", "<=", ">="})


def _init_db(pool):
    """Create tables and indexes if they do not exist (idempotent)."""
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS clicks (
                    id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_clicks_timestamp
                    ON clicks (timestamp);
            """)
        conn.commit()
    finally:
        pool.putconn(conn)


def _get_pool():
    """Return the connection pool, reinitialising after a fork."""
    global _pool, _pool_pid
    pid = os.getpid()
    if _pool is None or _pool_pid != pid:
        with _pool_lock:
            # Double-checked locking
            if _pool is None or _pool_pid != pid:
                if _pool is not None:
                    try:
                        _pool.closeall()
                    except Exception:
                        pass
                _pool = ThreadedConnectionPool(
                    minconn=POOL_MIN,
                    maxconn=POOL_MAX,
                    host=DB_HOST,
                    port=DB_PORT,
                    user=DB_USER,
                    password=DB_PASSWORD,
                    dbname=DB_NAME,
                )
                _pool_pid = pid
                _init_db(_pool)
    return _pool


def _get_conn():
    return _get_pool().getconn()


def _put_conn(conn):
    try:
        _get_pool().putconn(conn)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/click", methods=["POST"])
def register_click():
    """Register a new click. Returns 201 on success, 400 on failure."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO clicks DEFAULT VALUES")
        conn.commit()
        return "", 201
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return "", 400
    finally:
        _put_conn(conn)


@app.route("/clicks", methods=["GET"])
def retrieve_clicks():
    """Retrieve clicks filtered by date and direction."""
    date_str = request.args.get("date")
    direction = request.args.get("direction")

    if not date_str or not direction:
        return "", 400

    if direction not in VALID_DIRECTIONS:
        return "", 400

    # Validate date format
    try:
        parsed_date = date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return "", 400

    # direction is whitelisted → safe for string interpolation
    query = (
        "SELECT id, timestamp FROM clicks "
        f"WHERE timestamp {direction} %s "
        "ORDER BY timestamp DESC "
        "LIMIT %s"
    )

    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, (parsed_date.isoformat(), MAX_CLICKS))
            rows = cur.fetchall()

        if not rows:
            return "", 404

        results = [
            {"id": str(r["id"]), "timestamp": r["timestamp"].isoformat()}
            for r in rows
        ]
        return jsonify(results), 200
    except Exception:
        return "", 400
    finally:
        _put_conn(conn)


# ---------------------------------------------------------------------------
# Entry-point (for gunicorn: app:app)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
