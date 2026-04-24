import os
import uuid
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify

app = Flask(__name__)

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", 5432)),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", "postgres"),
    "dbname": os.environ.get("DB_NAME", "testdb"),
}


def get_db():
    return psycopg2.connect(**DB_CONFIG)


def init_db():
    try:
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS clicks (
                        id TEXT PRIMARY KEY,
                        timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


init_db()


@app.route("/click", methods=["POST"])
def register_click():
    click_id = str(uuid.uuid4())
    ts = datetime.now(timezone.utc)
    try:
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO clicks (id, timestamp) VALUES (%s, %s)",
                    (click_id, ts),
                )
            conn.commit()
        finally:
            conn.close()
        return jsonify({"id": click_id, "timestamp": ts.isoformat()}), 201
    except Exception:
        return jsonify({"error": "Invalid request"}), 400


@app.route("/clicks", methods=["GET"])
def get_clicks():
    date_str = request.args.get("date")
    direction = request.args.get("direction")

    if not date_str or not direction:
        return jsonify({"error": "Missing required parameters: date and direction"}), 400

    if direction not in ("<", ">", "<=", ">="):
        return jsonify({"error": "Invalid direction. Must be one of: <, >, <=, >="}), 400

    try:
        date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400

    op_map = {"<": "<", ">": ">", "<=": "<=", ">=": ">="}
    op = op_map[direction]

    try:
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT id, timestamp FROM clicks WHERE timestamp::date {op} %s ORDER BY timestamp",
                    (date,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        if not rows:
            return jsonify([]), 404

        result = [
            {"id": row[0], "timestamp": row[1].isoformat()}
            for row in rows
        ]
        return jsonify(result), 200
    except Exception:
        return jsonify({"error": "Invalid request"}), 400


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
