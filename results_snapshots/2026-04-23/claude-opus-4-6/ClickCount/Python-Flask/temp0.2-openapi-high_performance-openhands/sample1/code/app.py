import os
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.pool
from flask import Flask, jsonify, request

app = Flask(__name__)

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", 5432)),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", "postgres"),
    "dbname": os.environ.get("DB_NAME", "testdb"),
}

_pool = None


def get_pool():
    global _pool
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
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_clicks_timestamp ON clicks (timestamp);
            """)
    finally:
        conn.close()


init_db()

VALID_DIRECTIONS = frozenset({"<", ">", "<=", ">="})

# Map direction operators to timestamp range conditions for index-friendly queries.
# Instead of casting timestamp::date (which prevents index usage), compare against
# timestamp ranges derived from the date.
# For date-based comparison:
#   date < D   => timestamp < D (start of day D)
#   date <= D  => timestamp < D+1 (start of next day)
#   date > D   => timestamp >= D+1 (start of next day)
#   date >= D  => timestamp >= D (start of day D)


@app.route("/click", methods=["POST"])
def register_click():
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO clicks (id, timestamp) VALUES (gen_random_uuid(), NOW()) RETURNING id"
            )
            conn.commit()
        return "", 201
    except Exception:
        conn.rollback()
        return jsonify({"error": "Invalid request"}), 400
    finally:
        pool.putconn(conn)


@app.route("/clicks", methods=["GET"])
def get_clicks():
    date_str = request.args.get("date")
    direction = request.args.get("direction")

    if not date_str or not direction:
        return jsonify({"error": "Missing required parameters: date and direction"}), 400

    if direction not in VALID_DIRECTIONS:
        return jsonify({"error": "Invalid direction. Must be one of: <, >, <=, >="}), 400

    try:
        filter_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400

    # Convert date-level comparisons to timestamp-level for index usage
    next_day = filter_date + timedelta(days=1)
    if direction == "<":
        ts_op = "<"
        ts_val = filter_date
    elif direction == "<=":
        ts_op = "<"
        ts_val = next_day
    elif direction == ">":
        ts_op = ">="
        ts_val = next_day
    else:  # >=
        ts_op = ">="
        ts_val = filter_date

    query = f"SELECT id, timestamp FROM clicks WHERE timestamp {ts_op} %s ORDER BY timestamp"

    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(query, (ts_val,))
            rows = cur.fetchall()

        if not rows:
            return jsonify({"error": "No clicks found"}), 404

        results = [
            {
                "id": str(row[0]),
                "timestamp": row[1].isoformat(),
            }
            for row in rows
        ]
        return jsonify(results), 200
    except Exception:
        return jsonify({"error": "Invalid request"}), 400
    finally:
        pool.putconn(conn)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
