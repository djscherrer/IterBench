import os
import json
from datetime import datetime

from flask import Flask, request, jsonify
import psycopg2
import psycopg2.extras

app = Flask(__name__)

DB_HOST = os.environ.get('DB_HOST', 'localhost')
DB_PORT = os.environ.get('DB_PORT', '5432')
DB_USER = os.environ.get('DB_USER', 'postgres')
DB_PASSWORD = os.environ.get('DB_PASSWORD', 'postgres')
DB_NAME = os.environ.get('DB_NAME', 'petstore')


def get_db_connection():
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname=DB_NAME
    )
    conn.autocommit = False
    return conn


def init_db():
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # Use advisory lock to prevent concurrent initialization issues
        cur.execute("SELECT pg_advisory_lock(12345)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS pets (
                id BIGSERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                photo_urls TEXT NOT NULL DEFAULT '[]',
                status VARCHAR(20) DEFAULT 'available'
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id BIGSERIAL PRIMARY KEY,
                pet_id BIGINT,
                quantity INTEGER DEFAULT 0,
                ship_date TIMESTAMP,
                status VARCHAR(20) DEFAULT 'placed',
                complete BOOLEAN DEFAULT FALSE
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                username VARCHAR(255) UNIQUE NOT NULL,
                first_name VARCHAR(255),
                last_name VARCHAR(255),
                email VARCHAR(255),
                password VARCHAR(255),
                phone VARCHAR(255),
                user_status INTEGER DEFAULT 0
            );
        """)

        conn.commit()

        cur.execute("SELECT pg_advisory_unlock(12345)")
        conn.commit()
        cur.close()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


init_db()


# ---- Pet Endpoints ----

@app.route('/pet', methods=['POST'])
def add_pet():
    data = request.get_json()
    if not data or 'name' not in data or 'photoUrls' not in data:
        return jsonify({"message": "Invalid input"}), 400

    name = data['name']
    photo_urls = json.dumps(data.get('photoUrls', []))
    status = data.get('status', 'available')

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if 'id' in data and data['id'] is not None:
            cur.execute(
                "INSERT INTO pets (id, name, photo_urls, status) VALUES (%s, %s, %s, %s) RETURNING id",
                (data['id'], name, photo_urls, status)
            )
        else:
            cur.execute(
                "INSERT INTO pets (name, photo_urls, status) VALUES (%s, %s, %s) RETURNING id",
                (name, photo_urls, status)
            )
        pet_id = cur.fetchone()[0]
        conn.commit()
        cur.close()

        result = {
            "id": pet_id,
            "name": name,
            "photoUrls": data.get('photoUrls', []),
            "status": status
        }
        return jsonify(result), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"message": "Invalid input", "error": str(e)}), 400
    finally:
        conn.close()


@app.route('/pet', methods=['PUT'])
def update_pet():
    data = request.get_json()
    if not data or 'id' not in data:
        return jsonify({"message": "Invalid input"}), 400

    pet_id = data['id']
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM pets WHERE id = %s", (pet_id,))
        if cur.fetchone() is None:
            cur.close()
            return jsonify({"message": "Pet not found"}), 404

        name = data.get('name')
        photo_urls = json.dumps(data.get('photoUrls', []))
        status = data.get('status', 'available')

        cur.execute(
            "UPDATE pets SET name = %s, photo_urls = %s, status = %s WHERE id = %s",
            (name, photo_urls, status, pet_id)
        )
        conn.commit()
        cur.close()

        result = {
            "id": pet_id,
            "name": name,
            "photoUrls": data.get('photoUrls', []),
            "status": status
        }
        return jsonify(result), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"message": str(e)}), 400
    finally:
        conn.close()


@app.route('/pet/findByStatus', methods=['GET'])
def find_pets_by_status():
    status = request.args.get('status')
    if status not in ('available', 'pending', 'sold'):
        return jsonify([]), 200

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name, photo_urls, status FROM pets WHERE status = %s", (status,))
        rows = cur.fetchall()
        cur.close()
        conn.commit()

        pets = []
        for row in rows:
            pets.append({
                "id": row[0],
                "name": row[1],
                "photoUrls": json.loads(row[2]) if row[2] else [],
                "status": row[3]
            })
        return jsonify(pets), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"message": str(e)}), 500
    finally:
        conn.close()


@app.route('/pet/<int:petId>', methods=['GET'])
def get_pet_by_id(petId):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name, photo_urls, status FROM pets WHERE id = %s", (petId,))
        row = cur.fetchone()
        cur.close()
        conn.commit()

        if row is None:
            return jsonify({"message": "Pet not found"}), 404

        pet = {
            "id": row[0],
            "name": row[1],
            "photoUrls": json.loads(row[2]) if row[2] else [],
            "status": row[3]
        }
        return jsonify(pet), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"message": str(e)}), 500
    finally:
        conn.close()


@app.route('/pet/<int:petId>', methods=['DELETE'])
def delete_pet(petId):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM pets WHERE id = %s RETURNING id", (petId,))
        deleted = cur.fetchone()
        conn.commit()
        cur.close()

        if deleted is None:
            return jsonify({"message": "Pet not found"}), 404

        return jsonify({"message": "successful operation"}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"message": str(e)}), 500
    finally:
        conn.close()


# ---- Store/Order Endpoints ----

@app.route('/store/order', methods=['POST'])
def place_order():
    data = request.get_json()
    if not data:
        return jsonify({"message": "Invalid input"}), 400

    pet_id = data.get('petId')
    quantity = data.get('quantity', 0)
    ship_date = data.get('shipDate')
    status = data.get('status', 'placed')
    complete = data.get('complete', False)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if 'id' in data and data['id'] is not None:
            cur.execute(
                "INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (data['id'], pet_id, quantity, ship_date, status, complete)
            )
        else:
            cur.execute(
                "INSERT INTO orders (pet_id, quantity, ship_date, status, complete) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (pet_id, quantity, ship_date, status, complete)
            )
        order_id = cur.fetchone()[0]
        conn.commit()

        # Fetch the order back to get the stored ship_date
        cur.execute("SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id = %s", (order_id,))
        row = cur.fetchone()
        cur.close()

        result = {
            "id": row[0],
            "petId": row[1],
            "quantity": row[2],
            "shipDate": row[3].isoformat() if row[3] else None,
            "status": row[4],
            "complete": row[5]
        }
        return jsonify(result), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"message": str(e)}), 400
    finally:
        conn.close()


@app.route('/store/order/<int:orderId>', methods=['GET'])
def get_order_by_id(orderId):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id = %s", (orderId,))
        row = cur.fetchone()
        cur.close()
        conn.commit()

        if row is None:
            return jsonify({"message": "Order not found"}), 404

        result = {
            "id": row[0],
            "petId": row[1],
            "quantity": row[2],
            "shipDate": row[3].isoformat() if row[3] else None,
            "status": row[4],
            "complete": row[5]
        }
        return jsonify(result), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"message": str(e)}), 500
    finally:
        conn.close()


@app.route('/store/order/<int:orderId>', methods=['DELETE'])
def delete_order(orderId):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM orders WHERE id = %s RETURNING id", (orderId,))
        deleted = cur.fetchone()
        conn.commit()
        cur.close()

        if deleted is None:
            return jsonify({"message": "Order not found"}), 404

        return jsonify({"message": "successful operation"}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"message": str(e)}), 500
    finally:
        conn.close()


# ---- User Endpoints ----

@app.route('/user', methods=['POST'])
def create_user():
    data = request.get_json()
    if not data:
        return jsonify({"message": "Invalid input"}), 400

    username = data.get('username')
    first_name = data.get('firstName')
    last_name = data.get('lastName')
    email = data.get('email')
    password = data.get('password')
    phone = data.get('phone')
    user_status = data.get('userStatus', 0)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if 'id' in data and data['id'] is not None:
            cur.execute(
                """INSERT INTO users (id, username, first_name, last_name, email, password, phone, user_status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (data['id'], username, first_name, last_name, email, password, phone, user_status)
            )
        else:
            cur.execute(
                """INSERT INTO users (username, first_name, last_name, email, password, phone, user_status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (username, first_name, last_name, email, password, phone, user_status)
            )
        user_id = cur.fetchone()[0]
        conn.commit()
        cur.close()

        result = {
            "id": user_id,
            "username": username,
            "firstName": first_name,
            "lastName": last_name,
            "email": email,
            "password": password,
            "phone": phone,
            "userStatus": user_status
        }
        return jsonify(result), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"message": str(e)}), 400
    finally:
        conn.close()


@app.route('/user/login', methods=['GET'])
def login_user():
    username = request.args.get('username')
    password = request.args.get('password')

    if not username or not password:
        return jsonify({"message": "Invalid credentials"}), 400

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, password FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
        cur.close()
        conn.commit()

        if row is None:
            return jsonify({"message": "Invalid credentials"}), 400

        if row[1] != password:
            return jsonify({"message": "Invalid credentials"}), 400

        return jsonify("Logged in successfully"), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"message": str(e)}), 500
    finally:
        conn.close()


@app.route('/user/<username>', methods=['GET'])
def get_user_by_name(username):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username = %s",
            (username,)
        )
        row = cur.fetchone()
        cur.close()
        conn.commit()

        if row is None:
            return jsonify({"message": "User not found"}), 404

        result = {
            "id": row[0],
            "username": row[1],
            "firstName": row[2],
            "lastName": row[3],
            "email": row[4],
            "password": row[5],
            "phone": row[6],
            "userStatus": row[7]
        }
        return jsonify(result), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"message": str(e)}), 500
    finally:
        conn.close()


@app.route('/user/<username>', methods=['PUT'])
def update_user(username):
    data = request.get_json()
    if not data:
        return jsonify({"message": "Invalid input"}), 400

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        existing = cur.fetchone()

        if existing is None:
            cur.close()
            return jsonify({"message": "User not found"}), 404

        new_username = data.get('username', username)
        first_name = data.get('firstName')
        last_name = data.get('lastName')
        email = data.get('email')
        password = data.get('password')
        phone = data.get('phone')
        user_status = data.get('userStatus', 0)

        cur.execute(
            """UPDATE users SET username = %s, first_name = %s, last_name = %s, email = %s,
               password = %s, phone = %s, user_status = %s WHERE username = %s""",
            (new_username, first_name, last_name, email, password, phone, user_status, username)
        )
        conn.commit()

        cur.execute(
            "SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username = %s",
            (new_username,)
        )
        row = cur.fetchone()
        cur.close()

        result = {
            "id": row[0],
            "username": row[1],
            "firstName": row[2],
            "lastName": row[3],
            "email": row[4],
            "password": row[5],
            "phone": row[6],
            "userStatus": row[7]
        }
        return jsonify(result), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"message": str(e)}), 400
    finally:
        conn.close()


@app.route('/user/<username>', methods=['DELETE'])
def delete_user(username):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE username = %s RETURNING id", (username,))
        deleted = cur.fetchone()
        conn.commit()
        cur.close()

        if deleted is None:
            return jsonify({"message": "User not found"}), 404

        return jsonify({"message": "successful operation"}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"message": str(e)}), 500
    finally:
        conn.close()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)