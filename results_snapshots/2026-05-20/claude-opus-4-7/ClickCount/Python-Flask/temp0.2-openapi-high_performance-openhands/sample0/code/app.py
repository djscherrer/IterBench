import os
import threading
from datetime import datetime

from flask import Flask, request, jsonify
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "postgres")
DB_NAME = os.environ.get("DB_NAME", "testdb")

# Lazily initialized connection pool (per-process; safe with gunicorn --preload
# because pools are created after fork on first use).
_db_pool = None
_pool_lock = threading.Lock()


def get_pool():
    global _db_pool
    if _db_pool is None:
        with _pool_lock:
            if _db_pool is None:
                _db_pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=16,
                    host=DB_HOST,
                    port=DB_PORT,
                    user=DB_USER,
                    password=DB_PASSWORD,
                    dbname=DB_NAME,
                )
    return _db_pool


class PooledConn:
    def __enter__(self):
        self.conn = get_pool().getconn()
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if self.conn is not None:
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
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASSWORD, dbname=DB_NAME,
    )
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS clicks (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_clicks_timestamp "
                "ON clicks (timestamp);"
            )
    finally:
        conn.close()


# Initialize schema at import time. Safe under gunicorn --preload (single
# process), and idempotent thanks to IF NOT EXISTS.
try:
    init_db()
except Exception as e:
    app.logger.warning("Initial DB init failed: %s", e)


@app.route("/click", methods=["POST"])
def register_click():
    try:
        with PooledConn() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO clicks DEFAULT VALUES;")
            conn.commit()
        return ("", 201)
    except Exception:
        app.logger.exception("Failed to insert click")
        return jsonify({"error": "Invalid request"}), 400


@app.route("/clicks", methods=["GET"])
def get_clicks():
    date_str = request.args.get("date")
    direction = request.args.get("direction")

    if not date_str or direction not in ("<", "<=", ">", ">="):
        return jsonify({"error": "Invalid request"}), 400

    try:
        parsed_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Invalid request"}), 400

    # direction is whitelisted, safe to embed in SQL
    query = (
        f"SELECT id, timestamp FROM clicks "
        f"WHERE timestamp {direction} %s ORDER BY timestamp;"
    )

    try:
        with PooledConn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query, (parsed_date,))
                rows = cur.fetchall()
    except Exception:
        app.logger.exception("Failed to query clicks")
        return jsonify({"error": "Invalid request"}), 400

    if not rows:
        return jsonify({"error": "No clicks found"}), 404

    result = [
        {"id": str(r["id"]), "timestamp": r["timestamp"].isoformat()}
        for r in rows
    ]
    return jsonify(result), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port)
