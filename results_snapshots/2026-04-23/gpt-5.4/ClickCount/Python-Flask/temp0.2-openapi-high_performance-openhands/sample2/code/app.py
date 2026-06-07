import os
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone

from flask import Flask, jsonify, request
import psycopg2
from psycopg2.pool import ThreadedConnectionPool


app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "postgres")
PORT = int(os.environ.get("PORT", "5001"))
POOL_MIN_CONN = max(1, int(os.environ.get("DB_POOL_MIN_CONN", "1")))
POOL_MAX_CONN = max(POOL_MIN_CONN, int(os.environ.get("DB_POOL_MAX_CONN", "16")))

_pool = None
_pool_pid = None
_db_initialized = False
_db_init_lock = threading.Lock()


def _db_kwargs():
    return {
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "dbname": DB_NAME,
        "connect_timeout": 5,
        "application_name": "clickcount-app",
    }


def init_db():
    conn = psycopg2.connect(**_db_kwargs())
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS clicks (
                    id UUID PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_clicks_timestamp_id
                ON clicks (timestamp DESC, id)
                """
            )
    finally:
        conn.close()


def ensure_db_initialized():
    global _db_initialized
    if _db_initialized:
        return
    with _db_init_lock:
        if _db_initialized:
            return
        init_db()
        _db_initialized = True


def get_pool():
    global _pool, _pool_pid
    ensure_db_initialized()
    pid = os.getpid()
    if _pool is None or _pool_pid != pid:
        if _pool is not None:
            try:
                _pool.closeall()
            except Exception:
                pass
        _pool = ThreadedConnectionPool(POOL_MIN_CONN, POOL_MAX_CONN, **_db_kwargs())
        _pool_pid = pid
    return _pool


@contextmanager
def get_connection():
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)


@contextmanager
def get_cursor(commit=False):
    with get_connection() as conn:
        try:
            with conn.cursor() as cur:
                yield cur
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise


def format_timestamp(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def parse_click_filter(raw_date, direction):
    if not raw_date:
        return None, jsonify({"error": "date is required"}), 400
    if direction not in {"<", "<=", ">", ">="}:
        return None, jsonify({"error": "direction must be one of <, <=, >, >="}), 400

    try:
        target_date = date.fromisoformat(raw_date)
    except ValueError:
        return None, jsonify({"error": "date must use YYYY-MM-DD format"}), 400

    start = datetime.combine(target_date, time.min, tzinfo=timezone.utc)
    next_start = start + timedelta(days=1)

    mapping = {
        "<": ("timestamp < %s", (start,)),
        "<=": ("timestamp < %s", (next_start,)),
        ">": ("timestamp >= %s", (next_start,)),
        ">=": ("timestamp >= %s", (start,)),
    }
    return mapping[direction], None, None


@app.post("/click")
def register_click():
    click_id = uuid.uuid4()
    with get_cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO clicks (id) VALUES (%s) RETURNING id::text, timestamp",
            (str(click_id),),
        )
        stored_id, stored_timestamp = cur.fetchone()

    return (
        jsonify({"id": stored_id, "timestamp": format_timestamp(stored_timestamp)}),
        201,
    )


@app.get("/clicks")
def retrieve_clicks():
    filter_clause, error_response, status_code = parse_click_filter(
        request.args.get("date"), request.args.get("direction")
    )
    if error_response is not None:
        return error_response, status_code

    where_sql, params = filter_clause
    query = (
        "SELECT id::text, timestamp FROM clicks "
        f"WHERE {where_sql} "
        "ORDER BY timestamp DESC, id"
    )

    with get_cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    if not rows:
        return jsonify({"error": "no clicks found"}), 404

    payload = [
        {"id": click_id, "timestamp": format_timestamp(clicked_at)}
        for click_id, clicked_at in rows
    ]
    return jsonify(payload), 200


@app.errorhandler(psycopg2.Error)
def handle_database_error(_error):
    return jsonify({"error": "database unavailable"}), 503


@app.get("/health")
def healthcheck():
    return jsonify({"status": "ok"}), 200


try:
    ensure_db_initialized()
except psycopg2.Error:
    pass


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
