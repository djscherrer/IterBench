import os
import threading
import psycopg2
import psycopg2.pool
from flask import Flask, request, jsonify

app = Flask(__name__)

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", 5432)),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", "postgres"),
    "database": os.environ.get("DB_NAME", "testdb"),
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
                    **DB_CONFIG
                )
    return _pool


def get_conn():
    return get_pool().getconn()


def put_conn(conn):
    get_pool().putconn(conn)


def init_db():
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = True
        cur = conn.cursor()

        # Use advisory lock to prevent concurrent DDL issues
        cur.execute("SELECT pg_advisory_lock(12345)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS pets (
                id BIGSERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                photo_urls TEXT[] NOT NULL DEFAULT '{}',
                status VARCHAR(20) DEFAULT 'available'
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id BIGSERIAL PRIMARY KEY,
                pet_id BIGINT,
                quantity INTEGER DEFAULT 0,
                ship_date TIMESTAMPTZ,
                status VARCHAR(20) DEFAULT 'placed',
                complete BOOLEAN DEFAULT FALSE
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                username VARCHAR(255) UNIQUE NOT NULL,
                first_name VARCHAR(255) DEFAULT '',
                last_name VARCHAR(255) DEFAULT '',
                email VARCHAR(255) DEFAULT '',
                password VARCHAR(255) DEFAULT '',
                phone VARCHAR(50) DEFAULT '',
                user_status INTEGER DEFAULT 0
            )
        """)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_pets_status ON pets(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")

        cur.execute("SELECT pg_advisory_unlock(12345)")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Warning: Could not initialize database: {e}")


def pet_to_dict(row):
    return {
        "id": row[0],
        "name": row[1],
        "photoUrls": list(row[2]) if row[2] else [],
        "status": row[3],
    }


def order_to_dict(row):
    return {
        "id": row[0],
        "petId": row[1],
        "quantity": row[2],
        "shipDate": row[3].isoformat() if row[3] else None,
        "status": row[4],
        "complete": row[5],
    }


def user_to_dict(row):
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


# --- Pet endpoints ---

@app.route("/pet", methods=["POST"])
def add_pet():
    data = request.get_json()
    if not data or "name" not in data or "photoUrls" not in data:
        return jsonify({"message": "Invalid input"}), 400

    name = data["name"]
    photo_urls = data.get("photoUrls", [])
    status = data.get("status", "available")

    conn = get_conn()
    try:
        cur = conn.cursor()
        if "id" in data and data["id"] is not None:
            cur.execute(
                "INSERT INTO pets (id, name, photo_urls, status) VALUES (%s, %s, %s, %s) RETURNING id, name, photo_urls, status",
                (data["id"], name, photo_urls, status),
            )
        else:
            cur.execute(
                "INSERT INTO pets (name, photo_urls, status) VALUES (%s, %s, %s) RETURNING id, name, photo_urls, status",
                (name, photo_urls, status),
            )
        row = cur.fetchone()
        conn.commit()
        return jsonify(pet_to_dict(row)), 200
    except Exception:
        conn.rollback()
        return jsonify({"message": "Invalid input"}), 400
    finally:
        cur.close()
        put_conn(conn)


@app.route("/pet", methods=["PUT"])
def update_pet():
    data = request.get_json()
    if not data or "id" not in data:
        return jsonify({"message": "Invalid input"}), 400

    pet_id = data["id"]
    name = data.get("name", "")
    photo_urls = data.get("photoUrls", [])
    status = data.get("status", "available")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE pets SET name=%s, photo_urls=%s, status=%s WHERE id=%s RETURNING id, name, photo_urls, status",
            (name, photo_urls, status, pet_id),
        )
        row = cur.fetchone()
        conn.commit()
        if row is None:
            return jsonify({"message": "Pet not found"}), 404
        return jsonify(pet_to_dict(row)), 200
    except Exception:
        conn.rollback()
        return jsonify({"message": "Invalid input"}), 400
    finally:
        cur.close()
        put_conn(conn)


@app.route("/pet/findByStatus", methods=["GET"])
def find_pets_by_status():
    status = request.args.get("status")
    if status not in ("available", "pending", "sold"):
        return jsonify([]), 200

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, name, photo_urls, status FROM pets WHERE status=%s",
            (status,),
        )
        rows = cur.fetchall()
        return jsonify([pet_to_dict(r) for r in rows]), 200
    finally:
        cur.close()
        put_conn(conn)


@app.route("/pet/<int:petId>", methods=["GET"])
def get_pet_by_id(petId):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, name, photo_urls, status FROM pets WHERE id=%s", (petId,)
        )
        row = cur.fetchone()
        if row is None:
            return jsonify({"message": "Pet not found"}), 404
        return jsonify(pet_to_dict(row)), 200
    finally:
        cur.close()
        put_conn(conn)


@app.route("/pet/<int:petId>", methods=["DELETE"])
def delete_pet(petId):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM pets WHERE id=%s RETURNING id", (petId,))
        row = cur.fetchone()
        conn.commit()
        if row is None:
            return jsonify({"message": "Pet not found"}), 404
        return jsonify({"message": "successful operation"}), 200
    finally:
        cur.close()
        put_conn(conn)


# --- Store/Order endpoints ---

@app.route("/store/order", methods=["POST"])
def place_order():
    data = request.get_json()
    if not data:
        return jsonify({"message": "Invalid input"}), 400

    pet_id = data.get("petId")
    quantity = data.get("quantity", 0)
    ship_date = data.get("shipDate")
    status = data.get("status", "placed")
    complete = data.get("complete", False)

    conn = get_conn()
    try:
        cur = conn.cursor()
        if "id" in data and data["id"] is not None:
            cur.execute(
                "INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, pet_id, quantity, ship_date, status, complete",
                (data["id"], pet_id, quantity, ship_date, status, complete),
            )
        else:
            cur.execute(
                "INSERT INTO orders (pet_id, quantity, ship_date, status, complete) VALUES (%s, %s, %s, %s, %s) RETURNING id, pet_id, quantity, ship_date, status, complete",
                (pet_id, quantity, ship_date, status, complete),
            )
        row = cur.fetchone()
        conn.commit()
        return jsonify(order_to_dict(row)), 200
    except Exception:
        conn.rollback()
        return jsonify({"message": "Invalid input"}), 400
    finally:
        cur.close()
        put_conn(conn)


@app.route("/store/order/<int:orderId>", methods=["GET"])
def get_order_by_id(orderId):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id=%s",
            (orderId,),
        )
        row = cur.fetchone()
        if row is None:
            return jsonify({"message": "Order not found"}), 404
        return jsonify(order_to_dict(row)), 200
    finally:
        cur.close()
        put_conn(conn)


@app.route("/store/order/<int:orderId>", methods=["DELETE"])
def delete_order(orderId):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM orders WHERE id=%s RETURNING id", (orderId,))
        row = cur.fetchone()
        conn.commit()
        if row is None:
            return jsonify({"message": "Order not found"}), 404
        return jsonify({"message": "successful operation"}), 200
    finally:
        cur.close()
        put_conn(conn)


# --- User endpoints ---

@app.route("/user", methods=["POST"])
def create_user():
    data = request.get_json()
    if not data:
        return jsonify({"message": "Invalid input"}), 400

    username = data.get("username", "")
    first_name = data.get("firstName", "")
    last_name = data.get("lastName", "")
    email = data.get("email", "")
    password = data.get("password", "")
    phone = data.get("phone", "")
    user_status = data.get("userStatus", 0)

    conn = get_conn()
    try:
        cur = conn.cursor()
        if "id" in data and data["id"] is not None:
            cur.execute(
                "INSERT INTO users (id, username, first_name, last_name, email, password, phone, user_status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id, username, first_name, last_name, email, password, phone, user_status",
                (data["id"], username, first_name, last_name, email, password, phone, user_status),
            )
        else:
            cur.execute(
                "INSERT INTO users (username, first_name, last_name, email, password, phone, user_status) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id, username, first_name, last_name, email, password, phone, user_status",
                (username, first_name, last_name, email, password, phone, user_status),
            )
        row = cur.fetchone()
        conn.commit()
        return jsonify(user_to_dict(row)), 200
    except Exception:
        conn.rollback()
        return jsonify({"message": "Invalid input"}), 400
    finally:
        cur.close()
        put_conn(conn)


@app.route("/user/login", methods=["GET"])
def login_user():
    username = request.args.get("username")
    password = request.args.get("password")

    if not username or not password:
        return jsonify({"message": "Invalid credentials"}), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM users WHERE username=%s AND password=%s",
            (username, password),
        )
        row = cur.fetchone()
        if row is None:
            return jsonify({"message": "Invalid credentials"}), 400
        return jsonify("Logged in"), 200
    finally:
        cur.close()
        put_conn(conn)


@app.route("/user/<username>", methods=["GET"])
def get_user_by_name(username):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username=%s",
            (username,),
        )
        row = cur.fetchone()
        if row is None:
            return jsonify({"message": "User not found"}), 404
        return jsonify(user_to_dict(row)), 200
    finally:
        cur.close()
        put_conn(conn)


@app.route("/user/<username>", methods=["PUT"])
def update_user(username):
    data = request.get_json()
    if not data:
        return jsonify({"message": "Invalid input"}), 400

    new_username = data.get("username", username)
    first_name = data.get("firstName", "")
    last_name = data.get("lastName", "")
    email = data.get("email", "")
    password = data.get("password", "")
    phone = data.get("phone", "")
    user_status = data.get("userStatus", 0)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET username=%s, first_name=%s, last_name=%s, email=%s, password=%s, phone=%s, user_status=%s WHERE username=%s RETURNING id, username, first_name, last_name, email, password, phone, user_status",
            (new_username, first_name, last_name, email, password, phone, user_status, username),
        )
        row = cur.fetchone()
        conn.commit()
        if row is None:
            return jsonify({"message": "User not found"}), 404
        return jsonify(user_to_dict(row)), 200
    except Exception:
        conn.rollback()
        return jsonify({"message": "Invalid input"}), 400
    finally:
        cur.close()
        put_conn(conn)


@app.route("/user/<username>", methods=["DELETE"])
def delete_user(username):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE username=%s RETURNING id", (username,))
        row = cur.fetchone()
        conn.commit()
        if row is None:
            return jsonify({"message": "User not found"}), 404
        return jsonify({"message": "successful operation"}), 200
    finally:
        cur.close()
        put_conn(conn)


# Initialize DB on import (safe for concurrent workers due to IF NOT EXISTS)
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
