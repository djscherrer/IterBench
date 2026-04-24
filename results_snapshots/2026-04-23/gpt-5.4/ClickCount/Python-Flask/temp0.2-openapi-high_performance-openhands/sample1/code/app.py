import os
import uuid
from contextlib import contextmanager
from datetime import date as date_cls
from datetime import datetime, time, timedelta, timezone
from threading import Lock

import psycopg2
from flask import Flask, jsonify, request
from psycopg2.pool import ThreadedConnectionPool


app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

_POOL_LOCK = Lock()
_INIT_LOCK = Lock()
_DB_POOL = None
_DB_POOL_PID = None
_DB_READY = False
_ALLOWED_DIRECTIONS = {"<", "<=", ">", ">="}


def _db_settings():
    return {
        "host": os.environ["DB_HOST"],
        "port": int(os.getenv("DB_PORT", "5432")),
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
        "dbname": os.environ["DB_NAME"],
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "5")),
        "application_name": "click-tracking-api",
    }


def _pool_size():
    default_size = max(4, min(16, (os.cpu_count() or 1) * 2))
    configured = os.getenv("DB_POOL_MAX")
    if configured is None:
        return default_size

    try:
        size = int(configured)
    except ValueError:
        return default_size

    return max(1, size)


def initialize_database():
    ddl = """
    CREATE TABLE IF NOT EXISTS clicks (
        id UUID PRIMARY KEY,
        timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_clicks_timestamp_id ON clicks (timestamp, id);
    """

    with psycopg2.connect(**_db_settings()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(ddl)


def ensure_database_initialized():
    global _DB_READY

    if _DB_READY:
        return

    with _INIT_LOCK:
        if _DB_READY:
            return
        initialize_database()
        _DB_READY = True


def _get_pool():
    global _DB_POOL, _DB_POOL_PID

    ensure_database_initialized()

    pid = os.getpid()
    if _DB_POOL is not None and _DB_POOL_PID == pid:
        return _DB_POOL

    with _POOL_LOCK:
        if _DB_POOL is not None and _DB_POOL_PID != pid:
            try:
                _DB_POOL.closeall()
            except Exception:
                pass
            _DB_POOL = None

        if _DB_POOL is None:
            _DB_POOL = ThreadedConnectionPool(
                minconn=1,
                maxconn=_pool_size(),
                **_db_settings(),
            )
            _DB_POOL_PID = pid

        return _DB_POOL


@contextmanager
def _get_connection():
    pool = _get_pool()
    connection = pool.getconn()
    try:
        connection.autocommit = True
        yield connection
    finally:
        pool.putconn(connection)


def _serialize_click(row):
    click_id, click_timestamp = row
    if click_timestamp.tzinfo is None:
        click_timestamp = click_timestamp.replace(tzinfo=timezone.utc)
    return {
        "id": str(click_id),
        "timestamp": click_timestamp.isoformat(),
    }


def _parse_query_filter(raw_date, direction):
    if not raw_date or not direction:
        return None, jsonify({"error": "date and direction query parameters are required"}), 400

    if direction not in _ALLOWED_DIRECTIONS:
        return None, jsonify({"error": "direction must be one of <, <=, >, >="}), 400

    try:
        filter_date = date_cls.fromisoformat(raw_date)
    except ValueError:
        return None, jsonify({"error": "date must be a valid ISO 8601 date (YYYY-MM-DD)"}), 400

    start_of_day = datetime.combine(filter_date, time.min, tzinfo=timezone.utc)
    start_of_next_day = start_of_day + timedelta(days=1)

    if direction == "<":
        return ("timestamp < %s", (start_of_day,)), None, None
    if direction == "<=":
        return ("timestamp < %s", (start_of_next_day,)), None, None
    if direction == ">":
        return ("timestamp >= %s", (start_of_next_day,)), None, None
    return ("timestamp >= %s", (start_of_day,)), None, None


@app.post("/click")
def register_click():
    click_id = uuid.uuid4()
    query = "INSERT INTO clicks (id) VALUES (%s) RETURNING id, timestamp"

    with _get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, (click_id,))
            click = cursor.fetchone()

    return jsonify(_serialize_click(click)), 201


@app.get("/clicks")
def retrieve_clicks():
    filter_spec, error_response, status_code = _parse_query_filter(
        request.args.get("date"),
        request.args.get("direction"),
    )
    if error_response is not None:
        return error_response, status_code

    where_clause, params = filter_spec
    query = f"SELECT id, timestamp FROM clicks WHERE {where_clause} ORDER BY timestamp ASC, id ASC"

    with _get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()

    if not rows:
        return jsonify({"error": "no clicks found"}), 404

    return jsonify([_serialize_click(row) for row in rows]), 200


try:
    ensure_database_initialized()
except Exception as exc:
    app.logger.warning(
        "Database initialization on startup failed; requests will retry initialization: %s",
        exc,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5001")))
