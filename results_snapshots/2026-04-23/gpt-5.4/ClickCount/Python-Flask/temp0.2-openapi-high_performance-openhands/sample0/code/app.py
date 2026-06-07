import atexit
import os
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from threading import Lock

import psycopg2
from flask import Flask, jsonify, request
from psycopg2.pool import ThreadedConnectionPool


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("APP_SECRET", "")


_POOL = None
_POOL_PID = None
_POOL_LOCK = Lock()
_DB_READY = False
_DB_INIT_LOCK = Lock()


def _db_config():
    return {
        "host": os.environ["DB_HOST"],
        "port": int(os.environ.get("DB_PORT", "5432")),
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
        "dbname": os.environ["DB_NAME"],
        "connect_timeout": 5,
        "application_name": "clickcount_flask",
    }


def _pool_maxconn():
    raw_value = os.environ.get("DB_POOL_MAXCONN")
    if raw_value:
        try:
            return max(1, int(raw_value))
        except ValueError:
            pass
    cpu_count = os.cpu_count() or 1
    return max(4, min(32, cpu_count * 4))


def initialize_database(raise_errors=True):
    global _DB_READY
    if _DB_READY:
        return

    with _DB_INIT_LOCK:
        if _DB_READY:
            return
        try:
            conn = psycopg2.connect(**_db_config())
            try:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS clicks (
                            id BIGSERIAL PRIMARY KEY,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_clicks_created_at_id
                        ON clicks (created_at DESC, id DESC)
                        """
                    )
                _DB_READY = True
            finally:
                conn.close()
        except psycopg2.Error:
            if raise_errors:
                raise


initialize_database(raise_errors=False)


def _close_pool():
    global _POOL, _POOL_PID
    if _POOL is not None:
        _POOL.closeall()
        _POOL = None
        _POOL_PID = None


atexit.register(_close_pool)


def _get_pool():
    global _POOL, _POOL_PID
    initialize_database()

    current_pid = os.getpid()
    if _POOL is not None and _POOL_PID == current_pid:
        return _POOL

    with _POOL_LOCK:
        if _POOL is not None and _POOL_PID == current_pid:
            return _POOL
        if _POOL is not None:
            _POOL.closeall()
        _POOL = ThreadedConnectionPool(1, _pool_maxconn(), **_db_config())
        _POOL_PID = current_pid
        return _POOL


@contextmanager
def get_connection():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        conn.autocommit = True
        yield conn
    finally:
        pool.putconn(conn)


def json_error(message, status_code):
    response = jsonify({"error": message})
    response.status_code = status_code
    return response


def parse_filter_date(raw_date):
    try:
        return date.fromisoformat(raw_date)
    except (TypeError, ValueError):
        return None


def build_timestamp_filter(filter_date, direction):
    start_of_day = datetime.combine(filter_date, time.min, tzinfo=timezone.utc)
    next_day = start_of_day + timedelta(days=1)

    if direction == "<":
        return "created_at < %s", start_of_day
    if direction == "<=":
        return "created_at < %s", next_day
    if direction == ">":
        return "created_at >= %s", next_day
    if direction == ">=":
        return "created_at >= %s", start_of_day
    return None, None


def serialize_timestamp(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


@app.post("/click")
def register_click():
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO clicks DEFAULT VALUES RETURNING id, created_at")
                click_id, created_at = cur.fetchone()
    except psycopg2.Error:
        return json_error("Database unavailable", 503)

    response = jsonify(
        {
            "id": str(click_id),
            "timestamp": serialize_timestamp(created_at),
        }
    )
    response.status_code = 201
    return response


@app.get("/clicks")
def retrieve_clicks():
    filter_date = parse_filter_date(request.args.get("date"))
    if filter_date is None:
        return json_error("Invalid or missing 'date' query parameter", 400)

    direction = request.args.get("direction")
    where_clause, boundary = build_timestamp_filter(filter_date, direction)
    if where_clause is None:
        return json_error("Invalid or missing 'direction' query parameter", 400)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, created_at
                    FROM clicks
                    WHERE {where_clause}
                    ORDER BY created_at ASC, id ASC
                    """,
                    (boundary,),
                )
                rows = cur.fetchall()
    except psycopg2.Error:
        return json_error("Database unavailable", 503)

    if not rows:
        return json_error("No clicks found", 404)

    return jsonify(
        [
            {"id": str(click_id), "timestamp": serialize_timestamp(created_at)}
            for click_id, created_at in rows
        ]
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5001")))
