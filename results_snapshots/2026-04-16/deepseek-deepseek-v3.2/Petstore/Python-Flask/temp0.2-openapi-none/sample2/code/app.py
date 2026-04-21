import os
import json
import datetime
from typing import Dict, List, Optional, Any
from flask import Flask, request, jsonify
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool

app = Flask(__name__)

# Database connection pool
db_pool = None

def get_db_connection():
    """Get a database connection from the pool."""
    return db_pool.getconn()

def return_db_connection(conn):
    """Return a database connection to the pool."""
    db_pool.putconn(conn)

def init_db():
    """Initialize database tables if they don't exist."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Create pets table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pets (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    photo_urls TEXT[] DEFAULT '{}',
                    status VARCHAR(50) CHECK (status IN ('available', 'pending', 'sold'))
                )
            """)
            
            # Create orders table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    pet_id INTEGER,
                    quantity INTEGER,
                    ship_date TIMESTAMP,
                    status VARCHAR(50) CHECK (status IN ('placed', 'approved', 'delivered')),
                    complete BOOLEAN DEFAULT FALSE
                )
            """)
            
            # Create users table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(255) UNIQUE NOT NULL,
                    first_name VARCHAR(255),
                    last_name VARCHAR(255),
                    email VARCHAR(255),
                    password VARCHAR(255),
                    phone VARCHAR(50),
                    user_status INTEGER DEFAULT 0
                )
            """)
            
            conn.commit()
    finally:
        return_db_connection(conn)

@app.before_request
def before_first_request():
    """Initialize database connection pool and tables."""
    global db_pool
    if db_pool is None:
        db_pool = psycopg2.pool.SimpleConnectionPool(
            1, 20,
            host=os.environ.get('DB_HOST', 'localhost'),
            port=os.environ.get('DB_PORT', '5432'),
            user=os.environ.get('DB_USER', 'postgres'),
            password=os.environ.get('DB_PASSWORD', ''),
            database=os.environ.get('DB_NAME', 'petstore')
        )
        init_db()

# Pet endpoints
@app.route('/pet', methods=['POST'])
def add_pet():
    """Add a new pet to the store."""
    data = request.get_json()
    
    if not data or 'name' not in data or 'photoUrls' not in data:
        return jsonify({'error': 'Invalid input'}), 400
    
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO pets (name, photo_urls, status)
                VALUES (%s, %s, %s)
                RETURNING id, name, photo_urls as "photoUrls", status
                """,
                (data['name'], data['photoUrls'], data.get('status'))
            )
            pet = cur.fetchone()
            conn.commit()
            return jsonify(pet), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 400
    finally:
        return_db_connection(conn)

@app.route('/pet', methods=['PUT'])
def update_pet():
    """Update an existing pet."""
    data = request.get_json()
    
    if not data or 'id' not in data:
        return jsonify({'error': 'Invalid input'}), 400
    
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Check if pet exists
            cur.execute("SELECT id FROM pets WHERE id = %s", (data['id'],))
            if not cur.fetchone():
                return jsonify({'error': 'Pet not found'}), 404
            
            cur.execute(
                """
                UPDATE pets 
                SET name = %s, photo_urls = %s, status = %s
                WHERE id = %s
                RETURNING id, name, photo_urls as "photoUrls", status
                """,
                (data.get('name'), data.get('photoUrls'), data.get('status'), data['id'])
            )
            pet = cur.fetchone()
            conn.commit()
            return jsonify(pet), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 400
    finally:
        return_db_connection(conn)

@app.route('/pet/findByStatus', methods=['GET'])
def find_pets_by_status():
    """Finds Pets by status."""
    status = request.args.get('status')
    
    if not status or status not in ['available', 'pending', 'sold']:
        return jsonify({'error': 'Invalid status parameter'}), 400
    
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, name, photo_urls as "photoUrls", status
                FROM pets 
                WHERE status = %s
                """,
                (status,)
            )
            pets = cur.fetchall()
            return jsonify(pets), 200
    finally:
        return_db_connection(conn)

@app.route('/pet/<int:pet_id>', methods=['GET'])
def get_pet_by_id(pet_id):
    """Find pet by ID."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, name, photo_urls as "photoUrls", status
                FROM pets 
                WHERE id = %s
                """,
                (pet_id,)
            )
            pet = cur.fetchone()
            
            if not pet:
                return jsonify({'error': 'Pet not found'}), 404
            
            return jsonify(pet), 200
    finally:
        return_db_connection(conn)

@app.route('/pet/<int:pet_id>', methods=['DELETE'])
def delete_pet(pet_id):
    """Deletes a pet."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pets WHERE id = %s RETURNING id", (pet_id,))
            deleted = cur.fetchone()
            conn.commit()
            
            if not deleted:
                return jsonify({'error': 'Pet not found'}), 404
            
            return jsonify({'message': 'Pet deleted successfully'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 400
    finally:
        return_db_connection(conn)

# Store endpoints
@app.route('/store/order', methods=['POST'])
def place_order():
    """Place an order for a pet."""
    data = request.get_json()
    
    if not data:
        return jsonify({'error': 'Invalid input'}), 400
    
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Parse shipDate if provided
            ship_date = None
            if 'shipDate' in data and data['shipDate']:
                try:
                    ship_date = datetime.datetime.fromisoformat(data['shipDate'].replace('Z', '+00:00'))
                except ValueError:
                    ship_date = None
            
            cur.execute(
                """
                INSERT INTO orders (pet_id, quantity, ship_date, status, complete)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, pet_id as "petId", quantity, 
                          ship_date as "shipDate", status, complete
                """,
                (
                    data.get('petId'),
                    data.get('quantity'),
                    ship_date,
                    data.get('status'),
                    data.get('complete', False)
                )
            )
            order = cur.fetchone()
            conn.commit()
            
            # Convert datetime to ISO format string
            if order['shipDate']:
                order['shipDate'] = order['shipDate'].isoformat()
            
            return jsonify(order), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 400
    finally:
        return_db_connection(conn)

@app.route('/store/order/<int:order_id>', methods=['GET'])
def get_order_by_id(order_id):
    """Find purchase order by ID."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, pet_id as "petId", quantity, 
                       ship_date as "shipDate", status, complete
                FROM orders 
                WHERE id = %s
                """,
                (order_id,)
            )
            order = cur.fetchone()
            
            if not order:
                return jsonify({'error': 'Order not found'}), 404
            
            # Convert datetime to ISO format string
            if order['shipDate']:
                order['shipDate'] = order['shipDate'].isoformat()
            
            return jsonify(order), 200
    finally:
        return_db_connection(conn)

@app.route('/store/order/<int:order_id>', methods=['DELETE'])
def delete_order(order_id):
    """Delete purchase order by ID."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM orders WHERE id = %s RETURNING id", (order_id,))
            deleted = cur.fetchone()
            conn.commit()
            
            if not deleted:
                return jsonify({'error': 'Order not found'}), 404
            
            return jsonify({'message': 'Order deleted successfully'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 400
    finally:
        return_db_connection(conn)

# User endpoints
@app.route('/user', methods=['POST'])
def create_user():
    """Create user."""
    data = request.get_json()
    
    if not data or 'username' not in data:
        return jsonify({'error': 'Invalid input'}), 400
    
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO users (username, first_name, last_name, email, password, phone, user_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, username, first_name as "firstName", 
                          last_name as "lastName", email, password, phone, user_status as "userStatus"
                """,
                (
                    data['username'],
                    data.get('firstName'),
                    data.get('lastName'),
                    data.get('email'),
                    data.get('password'),
                    data.get('phone'),
                    data.get('userStatus', 0)
                )
            )
            user = cur.fetchone()
            conn.commit()
            return jsonify(user), 200
    except psycopg2.IntegrityError:
        conn.rollback()
        return jsonify({'error': 'Username already exists'}), 400
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 400
    finally:
        return_db_connection(conn)

@app.route('/user/<username>', methods=['GET'])
def get_user_by_name(username):
    """Get user by user name."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, username, first_name as "firstName", 
                       last_name as "lastName", email, password, phone, user_status as "userStatus"
                FROM users 
                WHERE username = %s
                """,
                (username,)
            )
            user = cur.fetchone()
            
            if not user:
                return jsonify({'error': 'User not found'}), 404
            
            return jsonify(user), 200
    finally:
        return_db_connection(conn)

@app.route('/user/<username>', methods=['PUT'])
def update_user(username):
    """Updated user."""
    data = request.get_json()
    
    if not data:
        return jsonify({'error': 'Invalid input'}), 400
    
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Check if user exists
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            if not cur.fetchone():
                return jsonify({'error': 'User not found'}), 404
            
            cur.execute(
                """
                UPDATE users 
                SET first_name = %s, last_name = %s, email = %s, 
                    password = %s, phone = %s, user_status = %s
                WHERE username = %s
                RETURNING id, username, first_name as "firstName", 
                          last_name as "lastName", email, password, phone, user_status as "userStatus"
                """,
                (
                    data.get('firstName'),
                    data.get('lastName'),
                    data.get('email'),
                    data.get('password'),
                    data.get('phone'),
                    data.get('userStatus', 0),
                    username
                )
            )
            user = cur.fetchone()
            conn.commit()
            return jsonify(user), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 400
    finally:
        return_db_connection(conn)

@app.route('/user/<username>', methods=['DELETE'])
def delete_user(username):
    """Delete user."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE username = %s RETURNING id", (username,))
            deleted = cur.fetchone()
            conn.commit()
            
            if not deleted:
                return jsonify({'error': 'User not found'}), 404
            
            return jsonify({'message': 'User deleted successfully'}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 400
    finally:
        return_db_connection(conn)

@app.route('/user/login', methods=['GET'])
def login_user():
    """Logs user into the system."""
    username = request.args.get('username')
    password = request.args.get('password')
    
    if not username or not password:
        return jsonify({'error': 'Invalid credentials'}), 400
    
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, username, first_name as "firstName", 
                       last_name as "lastName", email, password, phone, user_status as "userStatus"
                FROM users 
                WHERE username = %s AND password = %s
                """,
                (username, password)
            )
            user = cur.fetchone()
            
            if not user:
                return jsonify({'error': 'Invalid credentials'}), 400
            
            return jsonify(user), 200
    finally:
        return_db_connection(conn)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)