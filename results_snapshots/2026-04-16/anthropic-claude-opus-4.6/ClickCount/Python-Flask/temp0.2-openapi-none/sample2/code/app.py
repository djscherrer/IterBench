import os
import uuid
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify


def get_db_connection():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", "postgres"),
        database=os.environ.get("DB_NAME", "postgres"),
    )


def init_db():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS clicks (
                    id UUID PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
        conn.commit()
    finally:
        conn.close()


app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET", "default-secret")

# Initialize database tables (safe for concurrent workers due to IF NOT EXISTS)
init_db()


@app.route("/click", methods=["POST"])
def register_click():
    click_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO clicks (id, timestamp) VALUES (%s, %s)",
                (click_id, now),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        return jsonify({"error": "Invalid request"}), 400
    finally:
        conn.close()
    return jsonify({"message": "Click registered successfully"}), 201


@app.route("/clicks", methods=["GET"])
def get_clicks():
    date_str = request.args.get("date")
    direction = request.args.get("direction")

    if not date_str or not direction:
        return jsonify({"error": "Invalid request: 'date' and 'direction' query parameters are required"}), 400

    allowed_directions = {"<", ">", "<=", ">="}
    if direction not in allowed_directions:
        return jsonify({"error": f"Invalid direction. Must be one of {allowed_directions}"}), 400

    try:
        filter_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400

    # Map direction to SQL operator applied on timestamp::date
    op = direction
    query = f"SELECT id, timestamp FROM clicks WHERE timestamp::date {op} %s ORDER BY timestamp"

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (filter_date,))
            rows = cur.fetchall()
    except Exception:
        conn.rollback()
        return jsonify({"error": "Invalid request"}), 400
    finally:
        conn.close()

    if not rows:
        return jsonify({"error": "No clicks found"}), 404

    result = []
    for row in rows:
        result.append({
            "id": str(row["id"]),
            "timestamp": row["timestamp"].isoformat(),
        })

    return jsonify(result), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)