import json
import os

import psycopg2
import psycopg2.pool
from flask import Flask, Response, request, jsonify

app = Flask(__name__)

# ── Database configuration ──────────────────────────────────────────────────
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "dbname": os.environ.get("DB_NAME", "postgres"),
}

# ── Fork-safe connection pool ───────────────────────────────────────────────
_pool = None
_pool_pid = None


def _create_pool():
    return psycopg2.pool.ThreadedConnectionPool(
        minconn=4, maxconn=16, **DB_CONFIG
    )


def get_pool():
    global _pool, _pool_pid
    pid = os.getpid()
    if _pool is None or _pool_pid != pid:
        if _pool is not None:
            try:
                _pool.closeall()
            except Exception:
                pass
        _pool = _create_pool()
        _pool_pid = pid
        _init_db()
    return _pool


def _get_conn():
    return get_pool().getconn()


def _put_conn(conn):
    get_pool().putconn(conn)


# ── Database initialisation (idempotent) ────────────────────────────────────
def _init_db():
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pets (
                    id          BIGSERIAL PRIMARY KEY,
                    name        TEXT NOT NULL,
                    photo_urls  TEXT[] NOT NULL DEFAULT '{}',
                    status      TEXT DEFAULT 'available'
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id          BIGSERIAL PRIMARY KEY,
                    pet_id      BIGINT,
                    quantity    INTEGER,
                    ship_date   TEXT,
                    status      TEXT DEFAULT 'placed',
                    complete    BOOLEAN DEFAULT FALSE
                );

                CREATE TABLE IF NOT EXISTS users (
                    id          BIGSERIAL PRIMARY KEY,
                    username    TEXT UNIQUE NOT NULL,
                    first_name  TEXT,
                    last_name   TEXT,
                    email       TEXT,
                    password    TEXT,
                    phone       TEXT,
                    user_status INTEGER DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_pets_status   ON pets(status);
                CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
            """)
        conn.commit()
    finally:
        _put_conn(conn)


# ── Row marshallers ─────────────────────────────────────────────────────────
def _pet(row):
    return {
        "id": row[0],
        "name": row[1],
        "photoUrls": row[2] if row[2] else [],
        "status": row[3],
    }


def _order(row):
    return {
        "id": row[0],
        "petId": row[1],
        "quantity": row[2],
        "shipDate": row[3],
        "status": row[4],
        "complete": row[5],
    }


def _user(row):
    return {
        "id": row[0],
        "username": row[1],
        "firstName": row[2],
        "lastName": row[3],
        "email": row[4],
        "password": row[5],
        "phone": row[6],
        "userStatus": row[7],
    }


# ── Helpers ─────────────────────────────────────────────────────────────────
VALID_STATUSES = {"available", "pending", "sold"}


# ═════════════════════════════════════════════════════════════════════════════
#  PET endpoints
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/pet", methods=["POST"])
def add_pet():
    data = request.get_json(silent=True)
    if not data or "name" not in data or "photoUrls" not in data:
        return jsonify({"error": "Invalid input"}), 400

    name = data["name"]
    photo_urls = data.get("photoUrls", [])
    status = data.get("status", "available")
    if status not in VALID_STATUSES:
        status = "available"

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pets (name, photo_urls, status) "
                "VALUES (%s, %s, %s) "
                "RETURNING id, name, photo_urls, status",
                (name, photo_urls, status),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        _put_conn(conn)
    return jsonify(_pet(row)), 200


@app.route("/pet", methods=["PUT"])
def update_pet():
    data = request.get_json(silent=True)
    if not data or "id" not in data:
        return jsonify({"error": "Invalid input"}), 400

    pet_id = data["id"]
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, photo_urls, status FROM pets WHERE id = %s",
                (pet_id,),
            )
            existing = cur.fetchone()
            if not existing:
                return jsonify({"error": "Pet not found"}), 404

            name = data.get("name", existing[1])
            photo_urls = data.get("photoUrls", existing[2])
            status = data.get("status", existing[3])
            if status not in VALID_STATUSES:
                status = existing[3]

            cur.execute(
                "UPDATE pets SET name=%s, photo_urls=%s, status=%s "
                "WHERE id=%s "
                "RETURNING id, name, photo_urls, status",
                (name, photo_urls, status, pet_id),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        _put_conn(conn)
    return jsonify(_pet(row)), 200


@app.route("/pet/findByStatus", methods=["GET"])
def find_pets_by_status():
    status = request.args.get("status", "")
    if status not in VALID_STATUSES:
        return jsonify([]), 200

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, photo_urls, status FROM pets WHERE status = %s",
                (status,),
            )
            rows = cur.fetchall()
    finally:
        _put_conn(conn)
    return jsonify([_pet(r) for r in rows]), 200


@app.route("/pet/<int:pet_id>", methods=["GET"])
def get_pet_by_id(pet_id):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, photo_urls, status FROM pets WHERE id = %s",
                (pet_id,),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Pet not found"}), 404
    finally:
        _put_conn(conn)
    return jsonify(_pet(row)), 200


@app.route("/pet/<int:pet_id>", methods=["DELETE"])
def delete_pet(pet_id):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pets WHERE id = %s", (pet_id,))
            deleted = cur.rowcount
        conn.commit()
    finally:
        _put_conn(conn)
    if not deleted:
        return jsonify({"error": "Pet not found"}), 404
    return "", 200


# ═════════════════════════════════════════════════════════════════════════════
#  STORE / ORDER endpoints
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/store/order", methods=["POST"])
def place_order():
    data = request.get_json(silent=True) or {}
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO orders (pet_id, quantity, ship_date, status, complete) "
                "VALUES (%s, %s, %s, %s, %s) "
                "RETURNING id, pet_id, quantity, ship_date, status, complete",
                (
                    data.get("petId"),
                    data.get("quantity"),
                    data.get("shipDate"),
                    data.get("status", "placed"),
                    data.get("complete", False),
                ),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        _put_conn(conn)
    return jsonify(_order(row)), 200


@app.route("/store/order/<int:order_id>", methods=["GET"])
def get_order_by_id(order_id):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, pet_id, quantity, ship_date, status, complete "
                "FROM orders WHERE id = %s",
                (order_id,),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Order not found"}), 404
    finally:
        _put_conn(conn)
    return jsonify(_order(row)), 200


@app.route("/store/order/<int:order_id>", methods=["DELETE"])
def delete_order(order_id):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM orders WHERE id = %s", (order_id,))
            deleted = cur.rowcount
        conn.commit()
    finally:
        _put_conn(conn)
    if not deleted:
        return jsonify({"error": "Order not found"}), 404
    return "", 200


# ═════════════════════════════════════════════════════════════════════════════
#  USER endpoints
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/user", methods=["POST"])
def create_user():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid input"}), 400

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users "
                "(username, first_name, last_name, email, password, phone, user_status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id, username, first_name, last_name, email, password, phone, user_status",
                (
                    data.get("username"),
                    data.get("firstName"),
                    data.get("lastName"),
                    data.get("email"),
                    data.get("password"),
                    data.get("phone"),
                    data.get("userStatus", 0),
                ),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        _put_conn(conn)
    return jsonify(_user(row)), 200


@app.route("/user/<username>", methods=["GET"])
def get_user_by_name(username):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, first_name, last_name, email, password, phone, user_status "
                "FROM users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "User not found"}), 404
    finally:
        _put_conn(conn)
    return jsonify(_user(row)), 200


@app.route("/user/<username>", methods=["PUT"])
def update_user(username):
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid input"}), 400

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, first_name, last_name, email, password, phone, user_status "
                "FROM users WHERE username = %s",
                (username,),
            )
            existing = cur.fetchone()
            if not existing:
                return jsonify({"error": "User not found"}), 404

            cur.execute(
                "UPDATE users SET first_name=%s, last_name=%s, email=%s, "
                "password=%s, phone=%s, user_status=%s "
                "WHERE username=%s "
                "RETURNING id, username, first_name, last_name, email, password, phone, user_status",
                (
                    data.get("firstName", existing[2]),
                    data.get("lastName", existing[3]),
                    data.get("email", existing[4]),
                    data.get("password", existing[5]),
                    data.get("phone", existing[6]),
                    data.get("userStatus", existing[7]),
                    username,
                ),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        _put_conn(conn)
    return jsonify(_user(row)), 200


@app.route("/user/<username>", methods=["DELETE"])
def delete_user(username):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE username = %s", (username,))
            deleted = cur.rowcount
        conn.commit()
    finally:
        _put_conn(conn)
    if not deleted:
        return jsonify({"error": "User not found"}), 404
    return "", 200


@app.route("/user/login", methods=["GET"])
def login_user():
    username = request.args.get("username", "")
    password = request.args.get("password", "")

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT password FROM users WHERE username = %s", (username,)
            )
            row = cur.fetchone()
            if not row or row[0] != password:
                return jsonify({"error": "Invalid credentials"}), 400
    finally:
        _put_conn(conn)

    return Response(
        json.dumps("Logged in successfully"), mimetype="application/json"
    ), 200


# ── Entrypoint ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
