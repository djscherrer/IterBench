import os
import json
from datetime import datetime

from flask import Flask, request, jsonify, make_response
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from psycopg2 import errors


app = Flask(__name__)


DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "postgres")


connection_pool = None


def get_db_pool():
    global connection_pool
    if connection_pool is None:
        connection_pool = psycopg2.pool.SimpleConnectionPool(
            1,
            20,
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            dbname=DB_NAME,
        )
    return connection_pool


def get_db_connection():
    return get_db_pool().getconn()


def put_db_connection(conn):
    if conn is not None:
        get_db_pool().putconn(conn)


def parse_json_body():
    if not request.is_json:
        return None, make_response(jsonify({"message": "Invalid input"}), 400)
    data = request.get_json(silent=True)
    if data is None:
        return None, make_response(jsonify({"message": "Invalid input"}), 400)
    return data, None


def normalize_pet(row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "photoUrls": row["photo_urls"] if row["photo_urls"] is not None else [],
        "status": row["status"],
    }


def normalize_order(row):
    if row is None:
        return None
    ship_date = row["ship_date"]
    if isinstance(ship_date, datetime):
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


def initialize_database():
    conn = get_db_connection()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pets (
                    id BIGINT PRIMARY KEY,
                    name TEXT NOT NULL,
                    photo_urls TEXT[] NOT NULL DEFAULT '{}',
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
        conn.commit()
    finally:
        put_db_connection(conn)


def generate_id():
    return int(datetime.utcnow().timestamp() * 1000000)


@app.route("/pet", methods=["POST"])
def add_pet():
    data, error_response = parse_json_body()
    if error_response:
        return error_response

    name = data.get("name")
    photo_urls = data.get("photoUrls")
    status = data.get("status")

    if not name or not isinstance(photo_urls, list):
        return make_response(jsonify({"message": "Invalid input"}), 400)

    if status is not None and status not in ("available", "pending", "sold"):
        return make_response(jsonify({"message": "Invalid input"}), 400)

    pet_id = data.get("id")
    if pet_id is None:
        pet_id = generate_id()

    conn = get_db_connection()
    try:
        conn.autocommit = False
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO pets (id, name, photo_urls, status)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id, name, photo_urls, status
                    """,
                    (pet_id, name, photo_urls, status),
                )
                row = cur.fetchone()
                conn.commit()
                return jsonify(normalize_pet(row))
            except errors.UniqueViolation:
                conn.rollback()
                return make_response(jsonify({"message": "Invalid input"}), 400)
    finally:
        put_db_connection(conn)


@app.route("/pet", methods=["PUT"])
def update_pet():
    data, error_response = parse_json_body()
    if error_response:
        return error_response

    pet_id = data.get("id")
    name = data.get("name")
    photo_urls = data.get("photoUrls")
    status = data.get("status")

    if pet_id is None or not name or not isinstance(photo_urls, list):
        return make_response(jsonify({"message": "Invalid input"}), 400)

    if status is not None and status not in ("available", "pending", "sold"):
        return make_response(jsonify({"message": "Invalid input"}), 400)

    conn = get_db_connection()
    try:
        conn.autocommit = False
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM pets WHERE id = %s", (pet_id,))
            existing = cur.fetchone()
            if not existing:
                conn.rollback()
                return make_response(jsonify({"message": "Pet not found"}), 404)

            cur.execute(
                """
                UPDATE pets
                SET name = %s, photo_urls = %s, status = %s
                WHERE id = %s
                RETURNING id, name, photo_urls, status
                """,
                (name, photo_urls, status, pet_id),
            )
            row = cur.fetchone()
            conn.commit()
            return jsonify(normalize_pet(row))
    finally:
        put_db_connection(conn)


@app.route("/pet/findByStatus", methods=["GET"])
def find_pets_by_status():
    status = request.args.get("status")
    if status not in ("available", "pending", "sold"):
        return make_response(jsonify({"message": "Invalid status"}), 400)

    conn = get_db_connection()
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
            return jsonify([normalize_pet(row) for row in rows])
    finally:
        put_db_connection(conn)


@app.route("/pet/<int:pet_id>", methods=["GET"])
def get_pet_by_id(pet_id):
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, name, photo_urls, status FROM pets WHERE id = %s",
                (pet_id,),
            )
            row = cur.fetchone()
            if not row:
                return make_response(jsonify({"message": "Pet not found"}), 404)
            return jsonify(normalize_pet(row))
    finally:
        put_db_connection(conn)


@app.route("/pet/<int:pet_id>", methods=["DELETE"])
def delete_pet(pet_id):
    conn = get_db_connection()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pets WHERE id = %s", (pet_id,))
            deleted = cur.rowcount
            conn.commit()
            if deleted == 0:
                return make_response(jsonify({"message": "Pet not found"}), 404)
            return jsonify({"message": "successful operation"})
    finally:
        put_db_connection(conn)


@app.route("/store/order", methods=["POST"])
def place_order():
    data, error_response = parse_json_body()
    if error_response:
        return error_response

    order_id = data.get("id")
    if order_id is None:
        order_id = generate_id()

    pet_id = data.get("petId")
    quantity = data.get("quantity")
    ship_date_raw = data.get("shipDate")
    status = data.get("status")
    complete = data.get("complete")

    if status is not None and status not in ("placed", "approved", "delivered"):
        return make_response(jsonify({"message": "Invalid input"}), 400)

    ship_date = None
    if ship_date_raw is not None:
        try:
            ship_date = datetime.fromisoformat(ship_date_raw.replace("Z", "+00:00"))
        except ValueError:
            return make_response(jsonify({"message": "Invalid input"}), 400)

    conn = get_db_connection()
    try:
        conn.autocommit = False
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id, pet_id, quantity, ship_date, status, complete
                    """,
                    (order_id, pet_id, quantity, ship_date, status, complete),
                )
                row = cur.fetchone()
                conn.commit()
                return jsonify(normalize_order(row))
            except errors.UniqueViolation:
                conn.rollback()
                return make_response(jsonify({"message": "Invalid input"}), 400)
    finally:
        put_db_connection(conn)


@app.route("/store/order/<int:order_id>", methods=["GET"])
def get_order_by_id(order_id):
    conn = get_db_connection()
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
            if not row:
                return make_response(jsonify({"message": "Order not found"}), 404)
            return jsonify(normalize_order(row))
    finally:
        put_db_connection(conn)


@app.route("/store/order/<int:order_id>", methods=["DELETE"])
def delete_order(order_id):
    conn = get_db_connection()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("DELETE FROM orders WHERE id = %s", (order_id,))
            deleted = cur.rowcount
            conn.commit()
            if deleted == 0:
                return make_response(jsonify({"message": "Order not found"}), 404)
            return jsonify({"message": "successful operation"})
    finally:
        put_db_connection(conn)


@app.route("/user", methods=["POST"])
def create_user():
    data, error_response = parse_json_body()
    if error_response:
        return error_response

    username = data.get("username")
    if not username:
        return make_response(jsonify({"message": "Invalid input"}), 400)

    user_id = data.get("id")
    if user_id is None:
        user_id = generate_id()

    first_name = data.get("firstName")
    last_name = data.get("lastName")
    email = data.get("email")
    password = data.get("password")
    phone = data.get("phone")
    user_status = data.get("userStatus")

    conn = get_db_connection()
    try:
        conn.autocommit = False
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO users (
                        id, username, first_name, last_name, email, password, phone, user_status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, username, first_name, last_name, email, password, phone, user_status
                    """,
                    (
                        user_id,
                        username,
                        first_name,
                        last_name,
                        email,
                        password,
                        phone,
                        user_status,
                    ),
                )
                row = cur.fetchone()
                conn.commit()
                return jsonify(normalize_user(row))
            except errors.UniqueViolation:
                conn.rollback()
                return make_response(jsonify({"message": "Invalid input"}), 400)
    finally:
        put_db_connection(conn)


@app.route("/user/<string:username>", methods=["GET"])
def get_user_by_name(username):
    conn = get_db_connection()
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
            if not row:
                return make_response(jsonify({"message": "User not found"}), 404)
            return jsonify(normalize_user(row))
    finally:
        put_db_connection(conn)


@app.route("/user/<string:username>", methods=["PUT"])
def update_user(username):
    data, error_response = parse_json_body()
    if error_response:
        return error_response

    new_username = data.get("username")
    if not new_username:
        return make_response(jsonify({"message": "Invalid input"}), 400)

    user_id = data.get("id")
    first_name = data.get("firstName")
    last_name = data.get("lastName")
    email = data.get("email")
    password = data.get("password")
    phone = data.get("phone")
    user_status = data.get("userStatus")

    conn = get_db_connection()
    try:
        conn.autocommit = False
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            existing = cur.fetchone()
            if not existing:
                conn.rollback()
                return make_response(jsonify({"message": "User not found"}), 404)

            if user_id is None:
                user_id = existing["id"]

            try:
                cur.execute(
                    """
                    UPDATE users
                    SET id = %s,
                        username = %s,
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
                        user_id,
                        new_username,
                        first_name,
                        last_name,
                        email,
                        password,
                        phone,
                        user_status,
                        username,
                    ),
                )
                row = cur.fetchone()
                conn.commit()
                return jsonify(normalize_user(row))
            except errors.UniqueViolation:
                conn.rollback()
                return make_response(jsonify({"message": "Invalid input"}), 400)
    finally:
        put_db_connection(conn)


@app.route("/user/<string:username>", methods=["DELETE"])
def delete_user(username):
    conn = get_db_connection()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE username = %s", (username,))
            deleted = cur.rowcount
            conn.commit()
            if deleted == 0:
                return make_response(jsonify({"message": "User not found"}), 404)
            return jsonify({"message": "successful operation"})
    finally:
        put_db_connection(conn)


@app.route("/user/login", methods=["GET"])
def login_user():
    username = request.args.get("username")
    password = request.args.get("password")

    if not username or not password:
        return make_response(jsonify({"message": "Invalid credentials"}), 400)

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT username
                FROM users
                WHERE username = %s AND password = %s
                """,
                (username, password),
            )
            row = cur.fetchone()
            if not row:
                return make_response(jsonify({"message": "Invalid credentials"}), 400)
            return jsonify("logged in user session")
    finally:
        put_db_connection(conn)


@app.route("/", methods=["GET"])
def health():
    return jsonify({"message": "Petstore API"})


@app.errorhandler(404)
def not_found(_error):
    return make_response(jsonify({"message": "Not found"}), 404)


@app.errorhandler(405)
def method_not_allowed(_error):
    return make_response(jsonify({"message": "Method not allowed"}), 405)


@app.errorhandler(500)
def internal_error(_error):
    return make_response(jsonify({"message": "Internal server error"}), 500)


initialize_database()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port)