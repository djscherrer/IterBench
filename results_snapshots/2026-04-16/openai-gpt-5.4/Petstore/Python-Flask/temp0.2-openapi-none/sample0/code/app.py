import os
from datetime import datetime
from typing import Any, Dict, Optional, List

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, jsonify, request


app = Flask(__name__)


VALID_PET_STATUSES = {"available", "pending", "sold"}
VALID_ORDER_STATUSES = {"placed", "approved", "delivered"}


def get_db_connection():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST"),
        port=int(os.environ.get("DB_PORT", "5432")),
        user=os.environ.get("DB_USER"),
        password=os.environ.get("DB_PASSWORD"),
        dbname=os.environ.get("DB_NAME"),
        cursor_factory=RealDictCursor,
    )


def init_db():
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pets (
                        id BIGINT PRIMARY KEY,
                        name TEXT NOT NULL,
                        photo_urls JSONB NOT NULL,
                        status TEXT,
                        CONSTRAINT pets_status_check CHECK (status IS NULL OR status IN ('available', 'pending', 'sold'))
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
                        status TEXT,
                        complete BOOLEAN,
                        CONSTRAINT orders_status_check CHECK (status IS NULL OR status IN ('placed', 'approved', 'delivered'))
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
    finally:
        conn.close()


def row_to_pet(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "photoUrls": row["photo_urls"] if row["photo_urls"] is not None else [],
        "status": row["status"],
    }


def row_to_order(row: Dict[str, Any]) -> Dict[str, Any]:
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


def row_to_user(row: Dict[str, Any]) -> Dict[str, Any]:
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


def error_response(message: str, status_code: int):
    return jsonify({"error": message}), status_code


def get_json_body() -> Optional[Dict[str, Any]]:
    if not request.is_json:
        return None
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None
    return data


def parse_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("shipDate must be a string")
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError("shipDate must be a valid ISO 8601 date-time")


def validate_pet(data: Dict[str, Any], require_all: bool = True) -> Optional[str]:
    if require_all:
        if "name" not in data:
            return "Missing required field: name"
        if "photoUrls" not in data:
            return "Missing required field: photoUrls"
        if "id" not in data:
            return "Missing required field: id"

    if "id" in data and not isinstance(data["id"], int):
        return "Field 'id' must be an integer"

    if "name" in data and not isinstance(data["name"], str):
        return "Field 'name' must be a string"

    if "photoUrls" in data:
        if not isinstance(data["photoUrls"], list) or not all(isinstance(x, str) for x in data["photoUrls"]):
            return "Field 'photoUrls' must be an array of strings"

    if "status" in data and data["status"] is not None and data["status"] not in VALID_PET_STATUSES:
        return "Field 'status' must be one of: available, pending, sold"

    return None


def validate_order(data: Dict[str, Any], require_id: bool = True) -> Optional[str]:
    if require_id and "id" not in data:
        return "Missing required field: id"

    if "id" in data and not isinstance(data["id"], int):
        return "Field 'id' must be an integer"

    if "petId" in data and data["petId"] is not None and not isinstance(data["petId"], int):
        return "Field 'petId' must be an integer"

    if "quantity" in data and data["quantity"] is not None and not isinstance(data["quantity"], int):
        return "Field 'quantity' must be an integer"

    if "status" in data and data["status"] is not None and data["status"] not in VALID_ORDER_STATUSES:
        return "Field 'status' must be one of: placed, approved, delivered"

    if "complete" in data and data["complete"] is not None and not isinstance(data["complete"], bool):
        return "Field 'complete' must be a boolean"

    if "shipDate" in data and data["shipDate"] is not None:
        try:
            parse_datetime(data["shipDate"])
        except ValueError as exc:
            return str(exc)

    return None


def validate_user(data: Dict[str, Any], require_username: bool = True, require_id: bool = True) -> Optional[str]:
    if require_username and "username" not in data:
        return "Missing required field: username"
    if require_id and "id" not in data:
        return "Missing required field: id"

    string_fields = ["username", "firstName", "lastName", "email", "password", "phone"]
    for field in string_fields:
        if field in data and data[field] is not None and not isinstance(data[field], str):
            return f"Field '{field}' must be a string"

    if "id" in data and data["id"] is not None and not isinstance(data["id"], int):
        return "Field 'id' must be an integer"

    if "userStatus" in data and data["userStatus"] is not None and not isinstance(data["userStatus"], int):
        return "Field 'userStatus' must be an integer"

    return None


@app.route("/pet", methods=["POST"])
def add_pet():
    data = get_json_body()
    if data is None:
        return error_response("Invalid input", 400)

    validation_error = validate_pet(data, require_all=True)
    if validation_error:
        return error_response(validation_error, 400)

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM pets WHERE id = %s", (data["id"],))
                if cur.fetchone() is not None:
                    return error_response("Invalid input", 400)

                cur.execute(
                    """
                    INSERT INTO pets (id, name, photo_urls, status)
                    VALUES (%s, %s, %s::jsonb, %s)
                    RETURNING id, name, photo_urls, status
                    """,
                    (data["id"], data["name"], psycopg2.extras.Json(data["photoUrls"]), data.get("status")),
                )
                created = cur.fetchone()
                return jsonify(row_to_pet(created)), 200
    finally:
        conn.close()


@app.route("/pet", methods=["PUT"])
def update_pet():
    data = get_json_body()
    if data is None:
        return error_response("Invalid input", 400)

    validation_error = validate_pet(data, require_all=True)
    if validation_error:
        return error_response(validation_error, 400)

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM pets WHERE id = %s", (data["id"],))
                if cur.fetchone() is None:
                    return error_response("Pet not found", 404)

                cur.execute(
                    """
                    UPDATE pets
                    SET name = %s, photo_urls = %s::jsonb, status = %s
                    WHERE id = %s
                    RETURNING id, name, photo_urls, status
                    """,
                    (data["name"], psycopg2.extras.Json(data["photoUrls"]), data.get("status"), data["id"]),
                )
                updated = cur.fetchone()
                return jsonify(row_to_pet(updated)), 200
    finally:
        conn.close()


@app.route("/pet/findByStatus", methods=["GET"])
def find_pets_by_status():
    status = request.args.get("status")
    if status not in VALID_PET_STATUSES:
        return error_response("Invalid status value", 400)

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
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
            return jsonify([row_to_pet(row) for row in rows]), 200
    finally:
        conn.close()


@app.route("/pet/<int:pet_id>", methods=["GET"])
def get_pet_by_id(pet_id: int):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, photo_urls, status FROM pets WHERE id = %s",
                (pet_id,),
            )
            row = cur.fetchone()
            if row is None:
                return error_response("Pet not found", 404)
            return jsonify(row_to_pet(row)), 200
    finally:
        conn.close()


@app.route("/pet/<int:pet_id>", methods=["DELETE"])
def delete_pet(pet_id: int):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM pets WHERE id = %s RETURNING id", (pet_id,))
                deleted = cur.fetchone()
                if deleted is None:
                    return error_response("Pet not found", 404)
                return jsonify({"message": "successful operation"}), 200
    finally:
        conn.close()


@app.route("/store/order", methods=["POST"])
def place_order():
    data = get_json_body()
    if data is None:
        return error_response("Invalid input", 400)

    validation_error = validate_order(data, require_id=True)
    if validation_error:
        return error_response(validation_error, 400)

    ship_date = None
    if "shipDate" in data:
        try:
            ship_date = parse_datetime(data.get("shipDate"))
        except ValueError as exc:
            return error_response(str(exc), 400)

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM orders WHERE id = %s", (data["id"],))
                if cur.fetchone() is not None:
                    return error_response("Invalid input", 400)

                cur.execute(
                    """
                    INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id, pet_id, quantity, ship_date, status, complete
                    """,
                    (
                        data["id"],
                        data.get("petId"),
                        data.get("quantity"),
                        ship_date,
                        data.get("status"),
                        data.get("complete"),
                    ),
                )
                created = cur.fetchone()
                return jsonify(row_to_order(created)), 200
    finally:
        conn.close()


@app.route("/store/order/<int:order_id>", methods=["GET"])
def get_order_by_id(order_id: int):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
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
                return error_response("Order not found", 404)
            return jsonify(row_to_order(row)), 200
    finally:
        conn.close()


@app.route("/store/order/<int:order_id>", methods=["DELETE"])
def delete_order(order_id: int):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM orders WHERE id = %s RETURNING id", (order_id,))
                deleted = cur.fetchone()
                if deleted is None:
                    return error_response("Order not found", 404)
                return jsonify({"message": "successful operation"}), 200
    finally:
        conn.close()


@app.route("/user", methods=["POST"])
def create_user():
    data = get_json_body()
    if data is None:
        return error_response("Invalid input", 400)

    validation_error = validate_user(data, require_username=True, require_id=True)
    if validation_error:
        return error_response(validation_error, 400)

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE id = %s OR username = %s", (data["id"], data["username"]))
                if cur.fetchone() is not None:
                    return error_response("Invalid input", 400)

                cur.execute(
                    """
                    INSERT INTO users (id, username, first_name, last_name, email, password, phone, user_status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, username, first_name, last_name, email, password, phone, user_status
                    """,
                    (
                        data["id"],
                        data["username"],
                        data.get("firstName"),
                        data.get("lastName"),
                        data.get("email"),
                        data.get("password"),
                        data.get("phone"),
                        data.get("userStatus"),
                    ),
                )
                created = cur.fetchone()
                return jsonify(row_to_user(created)), 200
    finally:
        conn.close()


@app.route("/user/<string:username>", methods=["GET"])
def get_user_by_name(username: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
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
                return error_response("User not found", 404)
            return jsonify(row_to_user(row)), 200
    finally:
        conn.close()


@app.route("/user/<string:username>", methods=["PUT"])
def update_user(username: str):
    data = get_json_body()
    if data is None:
        return error_response("Invalid input", 400)

    validation_error = validate_user(data, require_username=True, require_id=True)
    if validation_error:
        return error_response(validation_error, 400)

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT username FROM users WHERE username = %s", (username,))
                existing = cur.fetchone()
                if existing is None:
                    return error_response("User not found", 404)

                cur.execute(
                    "SELECT username FROM users WHERE username = %s AND username <> %s",
                    (data["username"], username),
                )
                if cur.fetchone() is not None:
                    return error_response("Invalid input", 400)

                cur.execute(
                    "SELECT id FROM users WHERE id = %s AND username <> %s",
                    (data["id"], username),
                )
                if cur.fetchone() is not None:
                    return error_response("Invalid input", 400)

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
                        data["id"],
                        data["username"],
                        data.get("firstName"),
                        data.get("lastName"),
                        data.get("email"),
                        data.get("password"),
                        data.get("phone"),
                        data.get("userStatus"),
                        username,
                    ),
                )
                updated = cur.fetchone()
                return jsonify(row_to_user(updated)), 200
    finally:
        conn.close()


@app.route("/user/<string:username>", methods=["DELETE"])
def delete_user(username: str):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE username = %s RETURNING username", (username,))
                deleted = cur.fetchone()
                if deleted is None:
                    return error_response("User not found", 404)
                return jsonify({"message": "successful operation"}), 200
    finally:
        conn.close()


@app.route("/user/login", methods=["GET"])
def login_user():
    username = request.args.get("username")
    password = request.args.get("password")

    if not username or not password:
        return error_response("Invalid credentials", 400)

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT username
                FROM users
                WHERE username = %s AND password = %s
                """,
                (username, password),
            )
            row = cur.fetchone()
            if row is None:
                return error_response("Invalid credentials", 400)
            return jsonify("logged in user session"), 200
    finally:
        conn.close()


@app.route("/", methods=["GET"])
def health():
    return jsonify({"message": "Petstore API"}), 200


init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5001")))