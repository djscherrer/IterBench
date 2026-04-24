import os
import uuid
import threading
from datetime import datetime, timezone

import psycopg2
import psycopg2.pool
from flask import Flask, request, jsonify

app = Flask(__name__)

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", 5432)),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", "postgres"),
    "dbname": os.environ.get("DB_NAME", "testdb"),
}

_pool = None
_pool_lock = threading.Lock()


def get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=2,
                    maxconn=20,
                    **DB_CONFIG,
                )
    return _pool


def init_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS clicks (
                    id TEXT PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_clicks_timestamp ON clicks (timestamp);
            """)
    finally:
        conn.close()


try:
    init_db()
    get_pool()
except Exception:
    pass

DIRECTION_MAP = {
    "<": "<",
    ">": ">",
    "<=": "<=",
    ">=": ">=",
}


@app.route("/click", methods=["POST"])
def register_click():
    click_id = uuid.uuid4().hex
    p = get_pool()
    conn = p.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO clicks (id, timestamp) VALUES (%s, %s)",
                (click_id, datetime.now(timezone.utc)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        return jsonify({"error": "Invalid request"}), 400
    finally:
        p.putconn(conn)
    return jsonify({"id": click_id}), 201


@app.route("/clicks", methods=["GET"])
def get_clicks():
    date_str = request.args.get("date")
    direction = request.args.get("direction")

    if not date_str or not direction:
        return jsonify({"error": "Missing required parameters: date and direction"}), 400

    if direction not in DIRECTION_MAP:
        return jsonify({"error": "Invalid direction. Must be one of: <, >, <=, >="}), 400

    try:
        filter_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400

    op = DIRECTION_MAP[direction]
    query = f"SELECT id, timestamp FROM clicks WHERE timestamp {op} %s ORDER BY timestamp"

    p = get_pool()
    conn = p.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(query, (filter_date,))
            rows = cur.fetchall()
    finally:
        p.putconn(conn)

    if not rows:
        return jsonify([]), 404

    results = [
        {"id": row[0], "timestamp": row[1].isoformat()}
        for row in rows
    ]
    return jsonify(results), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
