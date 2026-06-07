import os
import uuid
from datetime import datetime, time
from flask import Flask, jsonify, request
import psycopg2
from psycopg2 import pool


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("APP_SECRET", "dev-secret")


DB_HOST = os.environ.get("DB_HOST")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_USER = os.environ.get("DB_USER")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
DB_NAME = os.environ.get("DB_NAME")

PORT = int(os.environ.get("PORT", "5001"))

ALLOWED_DIRECTIONS = {"<", ">", "<=", ">="}

_db_pool = None


def get_db_pool():
    global _db_pool
    if _db_pool is None:
        _db_pool = pool.SimpleConnectionPool(
            minconn=1,
            maxconn=10,
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            dbname=DB_NAME,
        )
    return _db_pool


def get_db_connection():
    return get_db_pool().getconn()


def release_db_connection(conn):
    get_db_pool().putconn(conn)


def init_db():
    conn = get_db_connection()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS clicks (
                    id UUID PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
        release_db_connection(conn)


def serialize_click(row):
    return {
        "id": str(row[0]),
        "timestamp": row[1].isoformat(),
    }


@app.route("/click", methods=["POST"])
def register_click():
    click_id = str(uuid.uuid4())

    conn = get_db_connection()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO clicks (id)
                VALUES (%s)
                RETURNING id, timestamp
                """,
                (click_id,),
            )
            row = cur.fetchone()
        return jsonify(serialize_click(row)), 201
    except Exception:
        return jsonify({"error": "Invalid request"}), 400
    finally:
        release_db_connection(conn)


@app.route("/clicks", methods=["GET"])
def retrieve_clicks():
    date_str = request.args.get("date")
    direction = request.args.get("direction")

    if not date_str or not direction:
        return jsonify({"error": "Missing required query parameters: date and direction"}), 400

    if direction not in ALLOWED_DIRECTIONS:
        return jsonify({"error": "Invalid direction. Allowed values are <, >, <=, >="}), 400

    try:
        parsed_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Invalid date format. Expected YYYY-MM-DD"}), 400

    filter_dt = datetime.combine(parsed_date, time.min)

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            query = f"""
                SELECT id, timestamp
                FROM clicks
                WHERE timestamp {direction} %s
                ORDER BY timestamp ASC
            """
            cur.execute(query, (filter_dt,))
            rows = cur.fetchall()

        if not rows:
            return jsonify({"error": "No clicks found"}), 404

        return jsonify([serialize_click(row) for row in rows]), 200
    except Exception:
        return jsonify({"error": "Invalid request"}), 400
    finally:
        release_db_connection(conn)


init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)