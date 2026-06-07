import os
import uuid
from datetime import datetime, time

import psycopg2
from flask import Flask, jsonify, request
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("APP_SECRET", "dev-secret")


def get_db_config():
    return {
        "host": os.environ["DB_HOST"],
        "port": int(os.environ.get("DB_PORT", 5432)),
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
        "dbname": os.environ["DB_NAME"],
    }


db_pool = pool.SimpleConnectionPool(1, 10, **get_db_config())


def get_conn():
    return db_pool.getconn()


def put_conn(conn):
    db_pool.putconn(conn)


def init_db():
    conn = get_conn()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS clicks (
                    id TEXT PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_clicks_timestamp
                ON clicks (timestamp)
                """
            )
    finally:
        put_conn(conn)


def parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def validate_direction(direction):
    return direction in {"<", ">", "<=", ">="}


@app.route("/click", methods=["POST"])
def register_click():
    click_id = str(uuid.uuid4())
    timestamp = datetime.utcnow()

    conn = get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO clicks (id, timestamp)
                    VALUES (%s, %s)
                    """,
                    (click_id, timestamp),
                )

        return (
            jsonify(
                {
                    "id": click_id,
                    "timestamp": timestamp.isoformat() + "Z",
                }
            ),
            201,
        )
    except Exception:
        conn.rollback()
        return jsonify({"error": "Invalid request"}), 400
    finally:
        put_conn(conn)


@app.route("/clicks", methods=["GET"])
def get_clicks():
    date_str = request.args.get("date")
    direction = request.args.get("direction")

    if not date_str or not direction:
        return jsonify({"error": "Missing required query parameters: date and direction"}), 400

    if not validate_direction(direction):
        return jsonify({"error": "Invalid direction. Must be one of: <, >, <=, >="}), 400

    parsed_date = parse_date(date_str)
    if parsed_date is None:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400

    filter_datetime = datetime.combine(parsed_date, time.min)

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = f"""
                SELECT id, timestamp
                FROM clicks
                WHERE timestamp {direction} %s
                ORDER BY timestamp ASC
            """
            cur.execute(query, (filter_datetime,))
            rows = cur.fetchall()

        if not rows:
            return jsonify({"error": "No clicks found"}), 404

        result = []
        for row in rows:
            ts = row["timestamp"]
            result.append(
                {
                    "id": row["id"],
                    "timestamp": ts.isoformat(),
                }
            )

        return jsonify(result), 200
    finally:
        put_conn(conn)


@app.errorhandler(400)
def handle_400(_error):
    return jsonify({"error": "Invalid request"}), 400


@app.errorhandler(404)
def handle_404(_error):
    return jsonify({"error": "Not found"}), 404


init_db()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)