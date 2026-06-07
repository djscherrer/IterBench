import os
import threading
from contextlib import contextmanager
from datetime import date, datetime, time, timezone

import psycopg2
from psycopg2 import pool
from psycopg2.pool import PoolError
from flask import Flask, jsonify, request


INIT_LOCK_ID = 902100431
VALID_DIRECTIONS = {"<", ">", "<=", ">="}

app = Flask(__name__)

_db_pool = None
_db_pool_pid = None
_db_initialized = False
_pool_lock = threading.Lock()
_init_lock = threading.Lock()


def _db_config():
    required = {
        "host": os.environ.get("DB_HOST"),
        "port": os.environ.get("DB_PORT"),
        "user": os.environ.get("DB_USER"),
        "password": os.environ.get("DB_PASSWORD"),
        "dbname": os.environ.get("DB_NAME"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        env_names = {
            "host": "DB_HOST",
            "port": "DB_PORT",
            "user": "DB_USER",
            "password": "DB_PASSWORD",
            "dbname": "DB_NAME",
        }
        raise RuntimeError(
            "Missing database environment variables: "
            + ", ".join(env_names[name] for name in missing)
        )
    required["connect_timeout"] = int(os.environ.get("DB_CONNECT_TIMEOUT", "5"))
    required["application_name"] = "click_tracking_api"
    return required


def _initialize_database():
    conn = psycopg2.connect(**_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (INIT_LOCK_ID,))
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS clicks (
                    id BIGSERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS clicks_timestamp_idx
                ON clicks (timestamp DESC)
                """
            )
            cur.execute("SELECT pg_advisory_unlock(%s)", (INIT_LOCK_ID,))
        conn.commit()
    except Exception:
        conn.rollback()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (INIT_LOCK_ID,))
            conn.commit()
        except Exception:
            conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_database_initialized(strict=False):
    global _db_initialized
    if _db_initialized:
        return
    with _init_lock:
        if _db_initialized:
            return
        try:
            _initialize_database()
            _db_initialized = True
        except Exception:
            app.logger.exception("database initialization failed")
            if strict:
                raise


def _get_pool():
    global _db_pool, _db_pool_pid
    _ensure_database_initialized(strict=True)
    pid = os.getpid()
    if _db_pool is None or _db_pool_pid != pid:
        with _pool_lock:
            if _db_pool is None or _db_pool_pid != pid:
                if _db_pool is not None:
                    try:
                        _db_pool.closeall()
                    except Exception:
                        pass
                maxconn = max(1, int(os.environ.get("DB_POOL_MAX", "8")))
                _db_pool = pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=maxconn,
                    **_db_config(),
                )
                _db_pool_pid = pid
    return _db_pool


@contextmanager
def _cursor():
    conn_pool = _get_pool()
    conn = None
    close_conn = False
    try:
        conn = conn_pool.getconn()
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except PoolError:
        raise
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                close_conn = True
        raise
    finally:
        if conn is not None:
            try:
                conn_pool.putconn(conn, close=close_conn or conn.closed != 0)
            except Exception:
                pass


def _json_error(message, status):
    return jsonify({"error": message}), status


def _parse_filter():
    raw_date = request.args.get("date")
    direction = request.args.get("direction")

    if not raw_date or not direction:
        return None, None, _json_error("date and direction query parameters are required", 400)
    if direction not in VALID_DIRECTIONS:
        return None, None, _json_error("direction must be one of <, >, <=, >=", 400)

    try:
        parsed_date = date.fromisoformat(raw_date)
    except ValueError:
        return None, None, _json_error("date must use YYYY-MM-DD format", 400)

    cutoff = datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)
    return cutoff, direction, None


def _format_timestamp(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@app.post("/click")
def register_click():
    try:
        with _cursor() as cur:
            cur.execute("INSERT INTO clicks DEFAULT VALUES RETURNING id::text, timestamp")
            click_id, timestamp = cur.fetchone()
    except PoolError:
        return _json_error("database connection pool exhausted", 503)
    except Exception:
        app.logger.exception("failed to register click")
        return _json_error("failed to register click", 500)

    return jsonify({"id": click_id, "timestamp": _format_timestamp(timestamp)}), 201


@app.get("/clicks")
def retrieve_clicks():
    cutoff, direction, error = _parse_filter()
    if error is not None:
        return error

    try:
        with _cursor() as cur:
            cur.execute(
                f"""
                SELECT id::text, timestamp
                FROM clicks
                WHERE timestamp {direction} %s
                ORDER BY timestamp DESC, id DESC
                """,
                (cutoff,),
            )
            rows = cur.fetchall()
    except PoolError:
        return _json_error("database connection pool exhausted", 503)
    except Exception:
        app.logger.exception("failed to retrieve clicks")
        return _json_error("failed to retrieve clicks", 500)

    if not rows:
        return _json_error("no clicks found", 404)

    return jsonify(
        [
            {"id": click_id, "timestamp": _format_timestamp(timestamp)}
            for click_id, timestamp in rows
        ]
    )


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


_ensure_database_initialized(strict=False)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False)
