import os
import json
from datetime import datetime

from flask import Flask, request, jsonify
import psycopg2
import psycopg2.extras

app = Flask(__name__)

def get_db_connection():
    conn = psycopg2.connect(
        host=os.environ.get('DB_HOST', 'localhost'),
        port=os.environ.get('DB_PORT', '5432'),
        user=os.environ.get('DB_USER', 'postgres'),
        password=os.environ.get('DB_PASSWORD', 'postgres'),
        database=os.environ.get('DB_NAME', 'petstore')
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
            )
        """)
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id BIGSERIAL PRIMARY KEY,
                pet_id BIGINT,
                quantity INTEGER DEFAULT 0,
                ship_date TIMESTAMP,
                status VARCHAR(20) DEFAULT 'placed',
                complete BOOLEAN DEFAULT FALSE
            )
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
            )
        """)
        
        conn.commit()
        cur.execute("SELECT pg_advisory_unlock(12345)")
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

init_db()

# ---- Pet endpoints ----

def pet_to_dict(row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "photoUrls": json.loads(row["photo_urls"]) if row["photo_urls"] else [],
        "status": row["status"]
    }

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
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if 'id' in data and data['id'] is not None:
            cur.execute(
                "INSERT INTO pets (id, name, photo_urls, status) VALUES (%s, %s, %s, %s) RETURNING *",
                (data['id'], name, photo_urls, status)
            )
        else:
            cur.execute(
                "INSERT INTO pets (name, photo_urls, status) VALUES (%s, %s, %s) RETURNING *",
                (name, photo_urls, status)
            )
        pet = cur.fetchone()
        conn.commit()
        return jsonify(pet_to_dict(pet)), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"message": "Invalid input"}), 400
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
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM pets WHERE id = %s", (pet_id,))
        existing = cur.fetchone()
        if not existing:
            conn.rollback()
            return jsonify({"message": "Pet not found"}), 404
        
        name = data.get('name', existing['name'])
        photo_urls = json.dumps(data.get('photoUrls', json.loads(existing['photo_urls'])))
        status = data.get('status', existing['status'])
        
        cur.execute(
            "UPDATE pets SET name = %s, photo_urls = %s, status = %s WHERE id = %s RETURNING *",
            (name, photo_urls, status, pet_id)
        )
        pet = cur.fetchone()
        conn.commit()
        return jsonify(pet_to_dict(pet)), 200
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
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM pets WHERE status = %s", (status,))
        pets = cur.fetchall()
        conn.commit()
        return jsonify([pet_to_dict(p) for p in pets]), 200
    finally:
        conn.close()

@app.route('/pet/<int:petId>', methods=['GET'])
def get_pet_by_id(petId):
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM pets WHERE id = %s", (petId,))
        pet = cur.fetchone()
        conn.commit()
        if not pet:
            return jsonify({"message": "Pet not found"}), 404
        return jsonify(pet_to_dict(pet)), 200
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
        if not deleted:
            return jsonify({"message": "Pet not found"}), 404
        return jsonify({"message": "successful operation"}), 200
    finally:
        conn.close()

# ---- Store/Order endpoints ----

def order_to_dict(row):
    if row is None:
        return None
    ship_date = row["ship_date"]
    if ship_date is not None:
        if isinstance(ship_date, datetime):
            ship_date = ship_date.isoformat()
        else:
            ship_date = str(ship_date)
    return {
        "id": row["id"],
        "petId": row["pet_id"],
        "quantity": row["quantity"],
        "shipDate": ship_date,
        "status": row["status"],
        "complete": row["complete"]
    }

@app.route('/store/order', methods=['POST'])
def place_order():
    data = request.get_json()
    if not data:
        return jsonify({"message": "Invalid input"}), 400
    
    pet_id = data.get('petId')
    quantity = data.get('quantity', 0)
    ship_date_str = data.get('shipDate')
    status = data.get('status', 'placed')
    complete = data.get('complete', False)
    
    ship_date = None
    if ship_date_str:
        try:
            ship_date = datetime.fromisoformat(ship_date_str.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            ship_date = None
    
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if 'id' in data and data['id'] is not None:
            cur.execute(
                "INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete) VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                (data['id'], pet_id, quantity, ship_date, status, complete)
            )
        else:
            cur.execute(
                "INSERT INTO orders (pet_id, quantity, ship_date, status, complete) VALUES (%s, %s, %s, %s, %s) RETURNING *",
                (pet_id, quantity, ship_date, status, complete)
            )
        order = cur.fetchone()
        conn.commit()
        return jsonify(order_to_dict(order)), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"message": str(e)}), 400
    finally:
        conn.close()

@app.route('/store/order/<int:orderId>', methods=['GET'])
def get_order_by_id(orderId):
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM orders WHERE id = %s", (orderId,))
        order = cur.fetchone()
        conn.commit()
        if not order:
            return jsonify({"message": "Order not found"}), 404
        return jsonify(order_to_dict(order)), 200
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
        if not deleted:
            return jsonify({"message": "Order not found"}), 404
        return jsonify({"message": "successful operation"}), 200
    finally:
        conn.close()

# ---- User endpoints ----

def user_to_dict(row):
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
        "userStatus": row["user_status"]
    }

@app.route('/user', methods=['POST'])
def create_user():
    data = request.get_json()
    if not data:
        return jsonify({"message": "Invalid input"}), 400
    
    username = data.get('username', '')
    first_name = data.get('firstName', '')
    last_name = data.get('lastName', '')
    email = data.get('email', '')
    password = data.get('password', '')
    phone = data.get('phone', '')
    user_status = data.get('userStatus', 0)
    
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if 'id' in data and data['id'] is not None:
            cur.execute(
                """INSERT INTO users (id, username, first_name, last_name, email, password, phone, user_status) 
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (data['id'], username, first_name, last_name, email, password, phone, user_status)
            )
        else:
            cur.execute(
                """INSERT INTO users (username, first_name, last_name, email, password, phone, user_status) 
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (username, first_name, last_name, email, password, phone, user_status)
            )
        user = cur.fetchone()
        conn.commit()
        return jsonify(user_to_dict(user)), 200
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
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM users WHERE username = %s AND password = %s", (username, password))
        user = cur.fetchone()
        conn.commit()
        if not user:
            return jsonify({"message": "Invalid credentials"}), 400
        return jsonify("Logged in successfully"), 200
    finally:
        conn.close()

@app.route('/user/<username>', methods=['GET'])
def get_user_by_name(username):
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
        conn.commit()
        if not user:
            return jsonify({"message": "User not found"}), 404
        return jsonify(user_to_dict(user)), 200
    finally:
        conn.close()

@app.route('/user/<username>', methods=['PUT'])
def update_user(username):
    data = request.get_json()
    if not data:
        return jsonify({"message": "Invalid input"}), 400
    
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        existing = cur.fetchone()
        if not existing:
            conn.rollback()
            return jsonify({"message": "User not found"}), 404
        
        new_username = data.get('username', existing['username'])
        first_name = data.get('firstName', existing['first_name'])
        last_name = data.get('lastName', existing['last_name'])
        email = data.get('email', existing['email'])
        password = data.get('password', existing['password'])
        phone = data.get('phone', existing['phone'])
        user_status = data.get('userStatus', existing['user_status'])
        
        cur.execute(
            """UPDATE users SET username = %s, first_name = %s, last_name = %s, 
               email = %s, password = %s, phone = %s, user_status = %s 
               WHERE username = %s RETURNING *""",
            (new_username, first_name, last_name, email, password, phone, user_status, username)
        )
        user = cur.fetchone()
        conn.commit()
        return jsonify(user_to_dict(user)), 200
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
        if not deleted:
            return jsonify({"message": "User not found"}), 404
        return jsonify({"message": "successful operation"}), 200
    finally:
        conn.close()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)