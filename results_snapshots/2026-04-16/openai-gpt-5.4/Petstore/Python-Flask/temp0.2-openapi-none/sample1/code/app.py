import os
from datetime import datetime
from flask import Flask, jsonify, request
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from psycopg2 import errors


app = Flask(__name__)


DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "postgres")


db_pool = None


def get_db_pool():
    global db_pool
    if db_pool is None:
        db_pool = pool.SimpleConnectionPool(
            minconn=1,
            maxconn=10,
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            dbname=DB_NAME,
        )
    return db_pool


def get_conn():
    return get_db_pool().getconn()


def put_conn(conn):
    get_db_pool().putconn(conn)


def row_to_dict(row):
    return dict(row) if row is not None else None


def parse_json_body():
    if not request.is_json:
        return None, (jsonify({"error": "Invalid input"}), 400)
    data = request.get_json(silent=True)
    if data is None:
        return None, (jsonify({"error": "Invalid input"}), 400)
    return data, None


def initialize_database():
    conn = get_conn()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pets (
                    id BIGINT PRIMARY KEY,
                    name TEXT NOT NULL,
                    photo_urls TEXT[] NOT NULL,
                    status TEXT CHECK (status IN ('available', 'pending', 'sold'))
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id BIGINT PRIMARY KEY,
                    pet_id BIGINT,
                    quantity INTEGER,
                    ship_date TIMESTAMPTZ,
                    status TEXT CHECK (status IN ('placed', 'approved', 'delivered')),
                    complete BOOLEAN
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGINT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    first_name TEXT,
                    last_name TEXT,
                    email TEXT,
                    password TEXT,
                    phone TEXT,
                    user_status INTEGER
                )
                """
            )

            cur.execute(
                """
                CREATE SEQUENCE IF NOT EXISTS pets_id_seq
                """
            )
            cur.execute(
                """
                CREATE SEQUENCE IF NOT EXISTS orders_id_seq
                """
            )
            cur.execute(
                """
                CREATE SEQUENCE IF NOT EXISTS users_id_seq
                """
            )
        conn.commit()
    finally:
        put_conn(conn)


def normalize_pet(row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "photoUrls": row["photo_urls"] or [],
        "status": row["status"],
    }


def normalize_order(row):
    if row is None:
        return None
    ship_date = row["ship_date"]
    if ship_date is not None:
        ship_date = ship_date.isoformat()
    return {
        "id": row["id"],
        "petId": row["pet_id"],
        "quantity": row["quantity"],
        "shipDate": ship_date,
        "status": row["status"],
        "complete": row["complete"],
    }


def normalize_user(row):
    if row is None:
        return None
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


def validate_pet_payload(data):
    if not isinstance(data, dict):
        return "Invalid input"
    if "name" not in data or "photoUrls" not in data:
        return "Invalid input"
    if not isinstance(data.get("name"), str):
        return "Invalid input"
    if not isinstance(data.get("photoUrls"), list) or not all(isinstance(x, str) for x in data.get("photoUrls", [])):
        return "Invalid input"
    if "status" in data and data["status"] is not None and data["status"] not in ("available", "pending", "sold"):
        return "Invalid input"
    return None


def validate_order_payload(data):
    if not isinstance(data, dict):
        return "Invalid input"
    if "status" in data and data["status"] is not None and data["status"] not in ("placed", "approved", "delivered"):
        return "Invalid input"
    return None


def validate_user_payload(data):
    if not isinstance(data, dict):
        return "Invalid input"
    return None


def next_id(conn, sequence_name):
    with conn.cursor() as cur:
        cur.execute("SELECT nextval(%s)", (sequence_name,))
        return cur.fetchone()[0]


@app.route("/pet", methods=["POST"])
def add_pet():
    data, error = parse_json_body()
    if error:
        return error

    validation_error = validate_pet_payload(data)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    conn = get_conn()
    try:
        conn.autocommit = False
        pet_id = data.get("id")
        if pet_id is None:
            pet_id = next_id(conn, "pets_id_seq")

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO pets (id, name, photo_urls, status)
                VALUES (%s, %s, %s, %s)
                RETURNING id, name, photo_urls, status
                """,
                (
                    pet_id,
                    data["name"],
                    data["photoUrls"],
                    data.get("status"),
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return jsonify(normalize_pet(row)), 200
    except errors.UniqueViolation:
        conn.rollback()
        return jsonify({"error": "Invalid input"}), 400
    finally:
        put_conn(conn)


@app.route("/pet", methods=["PUT"])
def update_pet():
    data, error = parse_json_body()
    if error:
        return error

    validation_error = validate_pet_payload(data)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    pet_id = data.get("id")
    if pet_id is None:
        return jsonify({"error": "Pet not found"}), 404

    conn = get_conn()
    try:
        conn.autocommit = False
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE pets
                SET name = %s, photo_urls = %s, status = %s
                WHERE id = %s
                RETURNING id, name, photo_urls, status
                """,
                (
                    data["name"],
                    data["photoUrls"],
                    data.get("status"),
                    pet_id,
                ),
            )
            row = cur.fetchone()
        conn.commit()

        if row is None:
            return jsonify({"error": "Pet not found"}), 404

        return jsonify(normalize_pet(row)), 200
    finally:
        put_conn(conn)


@app.route("/pet/findByStatus", methods=["GET"])
def find_pets_by_status():
    status = request.args.get("status")
    if status not in ("available", "pending", "sold"):
        return jsonify({"error": "Invalid input"}), 400

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, name, photo_urls, status
                FROM pets
                WHERE status = %s
                ORDER BY id
                """,
                (status,),
            )
            rows = cur.fetchall()
        return jsonify([normalize_pet(row) for row in rows]), 200
    finally:
        put_conn(conn)


@app.route("/pet/<int:pet_id>", methods=["GET"])
def get_pet_by_id(pet_id):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, name, photo_urls, status
                FROM pets
                WHERE id = %s
                """,
                (pet_id,),
            )
            row = cur.fetchone()
        if row is None:
            return jsonify({"error": "Pet not found"}), 404
        return jsonify(normalize_pet(row)), 200
    finally:
        put_conn(conn)


@app.route("/pet/<int:pet_id>", methods=["DELETE"])
def delete_pet(pet_id):
    conn = get_conn()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pets WHERE id = %s", (pet_id,))
            deleted = cur.rowcount
        conn.commit()

        if deleted == 0:
            return jsonify({"error": "Pet not found"}), 404
        return jsonify({"message": "successful operation"}), 200
    finally:
        put_conn(conn)


@app.route("/store/order", methods=["POST"])
def place_order():
    data, error = parse_json_body()
    if error:
        return error

    validation_error = validate_order_payload(data)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    ship_date = data.get("shipDate")
    if ship_date is not None:
        try:
            ship_date = datetime.fromisoformat(ship_date.replace("Z", "+00:00"))
        except ValueError:
            return jsonify({"error": "Invalid input"}), 400

    conn = get_conn()
    try:
        conn.autocommit = False
        order_id = data.get("id")
        if order_id is None:
            order_id = next_id(conn, "orders_id_seq")

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, pet_id, quantity, ship_date, status, complete
                """,
                (
                    order_id,
                    data.get("petId"),
                    data.get("quantity"),
                    ship_date,
                    data.get("status"),
                    data.get("complete"),
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return jsonify(normalize_order(row)), 200
    except errors.UniqueViolation:
        conn.rollback()
        return jsonify({"error": "Invalid input"}), 400
    finally:
        put_conn(conn)


@app.route("/store/order/<int:order_id>", methods=["GET"])
def get_order_by_id(order_id):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, pet_id, quantity, ship_date, status, complete
                FROM orders
                WHERE id = %s
                """,
                (order_id,),
            )
            row = cur.fetchone()
        if row is None:
            return jsonify({"error": "Order not found"}), 404
        return jsonify(normalize_order(row)), 200
    finally:
        put_conn(conn)


@app.route("/store/order/<int:order_id>", methods=["DELETE"])
def delete_order(order_id):
    conn = get_conn()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("DELETE FROM orders WHERE id = %s", (order_id,))
            deleted = cur.rowcount
        conn.commit()

        if deleted == 0:
            return jsonify({"error": "Order not found"}), 404
        return jsonify({"message": "successful operation"}), 200
    finally:
        put_conn(conn)


@app.route("/user", methods=["POST"])
def create_user():
    data, error = parse_json_body()
    if error:
        return error

    validation_error = validate_user_payload(data)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    username = data.get("username")
    if not username:
        return jsonify({"error": "Invalid input"}), 400

    conn = get_conn()
    try:
        conn.autocommit = False
        user_id = data.get("id")
        if user_id is None:
            user_id = next_id(conn, "users_id_seq")

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO users (id, username, first_name, last_name, email, password, phone, user_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, username, first_name, last_name, email, password, phone, user_status
                """,
                (
                    user_id,
                    data.get("username"),
                    data.get("firstName"),
                    data.get("lastName"),
                    data.get("email"),
                    data.get("password"),
                    data.get("phone"),
                    data.get("userStatus"),
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return jsonify(normalize_user(row)), 200
    except errors.UniqueViolation:
        conn.rollback()
        return jsonify({"error": "Invalid input"}), 400
    finally:
        put_conn(conn)


@app.route("/user/<string:username>", methods=["GET"])
def get_user_by_name(username):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, username, first_name, last_name, email, password, phone, user_status
                FROM users
                WHERE username = %s
                """,
                (username,),
            )
            row = cur.fetchone()
        if row is None:
            return jsonify({"error": "User not found"}), 404
        return jsonify(normalize_user(row)), 200
    finally:
        put_conn(conn)


@app.route("/user/<string:username>", methods=["PUT"])
def update_user(username):
    data, error = parse_json_body()
    if error:
        return error

    validation_error = validate_user_payload(data)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    new_username = data.get("username", username)
    if not new_username:
        return jsonify({"error": "Invalid input"}), 400

    conn = get_conn()
    try:
        conn.autocommit = False
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE users
                SET username = %s,
                    first_name = %s,
                    last_name = %s,
                    email = %s,
                    password = %s,
                    phone = %s,
                    user_status = %s
                WHERE username = %s
                RETURNING id, username, first_name, last_name, email, password, phone, user_status
                """,
                (
                    new_username,
                    data.get("firstName"),
                    data.get("lastName"),
                    data.get("email"),
                    data.get("password"),
                    data.get("phone"),
                    data.get("userStatus"),
                    username,
                ),
            )
            row = cur.fetchone()
        conn.commit()

        if row is None:
            return jsonify({"error": "User not found"}), 404

        return jsonify(normalize_user(row)), 200
    except errors.UniqueViolation:
        conn.rollback()
        return jsonify({"error": "Invalid input"}), 400
    finally:
        put_conn(conn)


@app.route("/user/<string:username>", methods=["DELETE"])
def delete_user(username):
    conn = get_conn()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE username = %s", (username,))
            deleted = cur.rowcount
        conn.commit()

        if deleted == 0:
            return jsonify({"error": "User not found"}), 404
        return jsonify({"message": "successful operation"}), 200
    finally:
        put_conn(conn)


@app.route("/user/login", methods=["GET"])
def login_user():
    username = request.args.get("username")
    password = request.args.get("password")

    if not username or not password:
        return jsonify({"error": "Invalid credentials"}), 400

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id
                FROM users
                WHERE username = %s AND password = %s
                """,
                (username, password),
            )
            row = cur.fetchone()

        if row is None:
            return jsonify({"error": "Invalid credentials"}), 400

        return jsonify("logged in user session"), 200
    finally:
        put_conn(conn)


@app.route("/", methods=["GET"])
def health():
    return jsonify({"message": "Petstore API"}), 200


initialize_database()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5001"))
    app.run(host="0.0.0.0", port=port)