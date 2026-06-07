import os
import uuid
from datetime import datetime, date, time, timezone

import psycopg2
from flask import Flask, jsonify, request


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("APP_SECRET", "dev-secret")


def get_db_connection():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 5432)),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        dbname=os.environ["DB_NAME"],
    )


def init_db():
    conn = get_db_connection()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS clicks (
                    id UUID PRIMARY KEY,
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
        conn.close()


# Safe under concurrent startup with gunicorn --preload and multiple workers:
# CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS are idempotent.
init_db()


@app.route("/click", methods=["POST"])
def register_click():
    click_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO clicks (id, timestamp)
                    VALUES (%s, %s)
                    """,
                    (click_id, now),
                )
        return (
            jsonify(
                {
                    "id": click_id,
                    "timestamp": now.isoformat(),
                }
            ),
            201,
        )
    except Exception:
        return jsonify({"error": "Invalid request"}), 400
    finally:
        conn.close()


@app.route("/clicks", methods=["GET"])
def retrieve_clicks():
    query_date = request.args.get("date")
    direction = request.args.get("direction")

    allowed_directions = {"<", ">", "<=", ">="}

    if not query_date or not direction:
        return jsonify({"error": "Missing required query parameters: date and direction"}), 400

    if direction not in allowed_directions:
        return jsonify({"error": "Invalid direction. Allowed values are <, >, <=, >="}), 400

    try:
        parsed_date = datetime.strptime(query_date, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Invalid date format. Expected YYYY-MM-DD"}), 400

    filter_dt = datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            sql = f"""
                SELECT id::text, timestamp
                FROM clicks
                WHERE timestamp {direction} %s
                ORDER BY timestamp ASC
            """
            cur.execute(sql, (filter_dt,))
            rows = cur.fetchall()

        if not rows:
            return jsonify({"error": "No clicks found"}), 404

        result = []
        for row in rows:
            result.append(
                {
                    "id": row[0],
                    "timestamp": row[1].astimezone(timezone.utc).isoformat(),
                }
            )

        return jsonify(result), 200
    except Exception:
        return jsonify({"error": "Invalid request"}), 400
    finally:
        conn.close()


@app.route("/", methods=["GET"])
def index():
    return jsonify(
        {
            "message": "Click Tracking API",
            "endpoints": {
                "POST /click": "Register a click",
                "GET /clicks?date=YYYY-MM-DD&direction=<|>|<=|>=": "Retrieve clicks",
            },
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)