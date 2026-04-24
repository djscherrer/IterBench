import os
import threading
from flask import Flask, request, jsonify, g
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import psycopg2

app = Flask(__name__)

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", 5432)),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", "postgres"),
    "database": os.environ.get("DB_NAME", "testdb"),
}

_pool_lock = threading.Lock()
_db_pool = None
_db_initialized = False


def get_pool():
    global _db_pool
    if _db_pool is not None:
        return _db_pool
    with _pool_lock:
        if _db_pool is not None:
            return _db_pool
        _ensure_tables()
        _db_pool = pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=20,
            **DB_CONFIG,
        )
        return _db_pool


def _ensure_tables():
    global _db_initialized
    if _db_initialized:
        return
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT pg_try_advisory_lock(12345)")
    got_lock = cur.fetchone()[0]
    if got_lock:
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pets (
                    id BIGSERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    photo_urls TEXT[] NOT NULL DEFAULT '{}',
                    status TEXT DEFAULT 'available'
                );
                CREATE INDEX IF NOT EXISTS idx_pets_status ON pets (status);

                CREATE TABLE IF NOT EXISTS orders (
                    id BIGSERIAL PRIMARY KEY,
                    pet_id BIGINT,
                    quantity INT DEFAULT 0,
                    ship_date TEXT,
                    status TEXT DEFAULT 'placed',
                    complete BOOLEAN DEFAULT FALSE
                );

                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    first_name TEXT DEFAULT '',
                    last_name TEXT DEFAULT '',
                    email TEXT DEFAULT '',
                    password TEXT DEFAULT '',
                    phone TEXT DEFAULT '',
                    user_status INT DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);
            """)
        finally:
            cur.execute("SELECT pg_advisory_unlock(12345)")
    _db_initialized = True
    cur.close()
    conn.close()


# Eagerly initialize if DB is reachable (for --preload)
try:
    get_pool()
except Exception:
    pass


def get_conn():
    if "db_conn" not in g:
        g.db_conn = get_pool().getconn()
    return g.db_conn


@app.teardown_appcontext
def return_conn(exc):
    conn = g.pop("db_conn", None)
    if conn is not None:
        conn.rollback()
        get_pool().putconn(conn)


# --- Pet endpoints ---

@app.route("/pet", methods=["POST"])
def add_pet():
    data = request.get_json(silent=True)
    if not data or "name" not in data or "photoUrls" not in data:
        return jsonify({"message": "Invalid input"}), 400
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "INSERT INTO pets (name, photo_urls, status) VALUES (%s, %s, %s) RETURNING id, name, photo_urls, status",
        (data["name"], data.get("photoUrls", []), data.get("status", "available")),
    )
    pet = cur.fetchone()
    conn.commit()
    cur.close()
    return jsonify(_format_pet(pet))


@app.route("/pet", methods=["PUT"])
def update_pet():
    data = request.get_json(silent=True)
    if not data or "id" not in data:
        return jsonify({"message": "Invalid input"}), 400
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "UPDATE pets SET name=%s, photo_urls=%s, status=%s WHERE id=%s RETURNING id, name, photo_urls, status",
        (data.get("name", ""), data.get("photoUrls", []), data.get("status", "available"), data["id"]),
    )
    pet = cur.fetchone()
    conn.commit()
    cur.close()
    if pet is None:
        return jsonify({"message": "Pet not found"}), 404
    return jsonify(_format_pet(pet))


@app.route("/pet/findByStatus", methods=["GET"])
def find_pets_by_status():
    status = request.args.get("status", "available")
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, name, photo_urls, status FROM pets WHERE status=%s", (status,))
    pets = cur.fetchall()
    cur.close()
    return jsonify([_format_pet(p) for p in pets])


@app.route("/pet/<int:pet_id>", methods=["GET"])
def get_pet(pet_id):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, name, photo_urls, status FROM pets WHERE id=%s", (pet_id,))
    pet = cur.fetchone()
    cur.close()
    if pet is None:
        return jsonify({"message": "Pet not found"}), 404
    return jsonify(_format_pet(pet))


@app.route("/pet/<int:pet_id>", methods=["DELETE"])
def delete_pet(pet_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM pets WHERE id=%s RETURNING id", (pet_id,))
    deleted = cur.fetchone()
    conn.commit()
    cur.close()
    if deleted is None:
        return jsonify({"message": "Pet not found"}), 404
    return jsonify({"message": "successful operation"})


def _format_pet(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "photoUrls": list(row["photo_urls"]) if row["photo_urls"] else [],
        "status": row["status"],
    }


# --- Order endpoints ---

@app.route("/store/order", methods=["POST"])
def place_order():
    data = request.get_json(silent=True) or {}
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "INSERT INTO orders (pet_id, quantity, ship_date, status, complete) VALUES (%s, %s, %s, %s, %s) RETURNING *",
        (
            data.get("petId"),
            data.get("quantity", 0),
            data.get("shipDate"),
            data.get("status", "placed"),
            data.get("complete", False),
        ),
    )
    order = cur.fetchone()
    conn.commit()
    cur.close()
    return jsonify(_format_order(order))


@app.route("/store/order/<int:order_id>", methods=["GET"])
def get_order(order_id):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    order = cur.fetchone()
    cur.close()
    if order is None:
        return jsonify({"message": "Order not found"}), 404
    return jsonify(_format_order(order))


@app.route("/store/order/<int:order_id>", methods=["DELETE"])
def delete_order(order_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM orders WHERE id=%s RETURNING id", (order_id,))
    deleted = cur.fetchone()
    conn.commit()
    cur.close()
    if deleted is None:
        return jsonify({"message": "Order not found"}), 404
    return jsonify({"message": "successful operation"})


def _format_order(row):
    return {
        "id": row["id"],
        "petId": row["pet_id"],
        "quantity": row["quantity"],
        "shipDate": row["ship_date"],
        "status": row["status"],
        "complete": row["complete"],
    }


# --- User endpoints ---

@app.route("/user", methods=["POST"])
def create_user():
    data = request.get_json(silent=True) or {}
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """INSERT INTO users (username, first_name, last_name, email, password, phone, user_status)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (
            data.get("username", ""),
            data.get("firstName", ""),
            data.get("lastName", ""),
            data.get("email", ""),
            data.get("password", ""),
            data.get("phone", ""),
            data.get("userStatus", 0),
        ),
    )
    user = cur.fetchone()
    conn.commit()
    cur.close()
    return jsonify(_format_user(user))


@app.route("/user/login", methods=["GET"])
def login_user():
    username = request.args.get("username")
    password = request.args.get("password")
    if not username or not password:
        return jsonify({"message": "Invalid credentials"}), 400
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM users WHERE username=%s AND password=%s", (username, password))
    user = cur.fetchone()
    cur.close()
    if user is None:
        return jsonify({"message": "Invalid credentials"}), 400
    return jsonify("Logged in successfully")


@app.route("/user/<username>", methods=["GET"])
def get_user(username):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM users WHERE username=%s", (username,))
    user = cur.fetchone()
    cur.close()
    if user is None:
        return jsonify({"message": "User not found"}), 404
    return jsonify(_format_user(user))


@app.route("/user/<username>", methods=["PUT"])
def update_user(username):
    data = request.get_json(silent=True) or {}
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """UPDATE users SET username=%s, first_name=%s, last_name=%s, email=%s,
           password=%s, phone=%s, user_status=%s WHERE username=%s RETURNING *""",
        (
            data.get("username", username),
            data.get("firstName", ""),
            data.get("lastName", ""),
            data.get("email", ""),
            data.get("password", ""),
            data.get("phone", ""),
            data.get("userStatus", 0),
            username,
        ),
    )
    user = cur.fetchone()
    conn.commit()
    cur.close()
    if user is None:
        return jsonify({"message": "User not found"}), 404
    return jsonify(_format_user(user))


@app.route("/user/<username>", methods=["DELETE"])
def delete_user(username):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE username=%s RETURNING id", (username,))
    deleted = cur.fetchone()
    conn.commit()
    cur.close()
    if deleted is None:
        return jsonify({"message": "User not found"}), 404
    return jsonify({"message": "successful operation"})


def _format_user(row):
    return {
        "id": row["id"],
        "username": row["username"],
        "firstName": row["first_name"],
        "lastName": row["last_name"],
        "email": row["email"],
        "password": row["password"],
        "phone": row["phone"],
        "userStatus": row["user_status"],
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
