import os
import json
import logging
from datetime import datetime
from contextlib import contextmanager
from typing import Dict, List, Optional, Any

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool
from flask import Flask, request, jsonify, abort

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)

# Database connection pool
connection_pool = None

def get_db_config():
    """Get database configuration from environment variables."""
    return {
        'host': os.environ.get('DB_HOST', 'localhost'),
        'port': os.environ.get('DB_PORT', '5432'),
        'user': os.environ.get('DB_USER', 'postgres'),
        'password': os.environ.get('DB_PASSWORD', ''),
        'database': os.environ.get('DB_NAME', 'petstore')
    }

def init_connection_pool():
    """Initialize the database connection pool."""
    global connection_pool
    db_config = get_db_config()
    
    # Create connection pool with 1-10 connections
    connection_pool = SimpleConnectionPool(
        1, 10,
        host=db_config['host'],
        port=db_config['port'],
        user=db_config['user'],
        password=db_config['password'],
        database=db_config['database']
    )
    logger.info("Database connection pool initialized")

@contextmanager
def get_db_connection():
    """Get a database connection from the pool."""
    conn = None
    try:
        conn = connection_pool.getconn()
        yield conn
    finally:
        if conn:
            connection_pool.putconn(conn)

@contextmanager
def get_db_cursor():
    """Get a database cursor with RealDictCursor for dictionary results."""
    with get_db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            yield cursor
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            cursor.close()

def init_database():
    """Initialize database tables if they don't exist."""
    try:
        with get_db_cursor() as cursor:
            # Create pets table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pets (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    photo_urls JSONB NOT NULL,
                    status VARCHAR(50) CHECK (status IN ('available', 'pending', 'sold')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Create users table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(255) UNIQUE NOT NULL,
                    first_name VARCHAR(255),
                    last_name VARCHAR(255),
                    email VARCHAR(255),
                    password VARCHAR(255),
                    phone VARCHAR(50),
                    user_status INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Create orders table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    pet_id INTEGER NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    ship_date TIMESTAMP,
                    status VARCHAR(50) CHECK (status IN ('placed', 'approved', 'delivered')),
                    complete BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (pet_id) REFERENCES pets(id) ON DELETE CASCADE
                )
            """)
            
            logger.info("Database tables initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise

# Initialize app
@app.before_first_request
def initialize():
    """Initialize database connection pool and tables."""
    try:
        init_connection_pool()
        init_database()
    except Exception as e:
        logger.error(f"Initialization failed: {e}")
        # Don't raise here to allow app to start even if DB is temporarily unavailable

# Pet endpoints
@app.route('/pet', methods=['POST'])
def add_pet():
    """Add a new pet to the store."""
    try:
        data = request.get_json()
        
        # Validate required fields
        if not data or 'name' not in data or 'photoUrls' not in data:
            abort(400, description="Invalid input: name and photoUrls are required")
        
        # Validate status if provided
        if 'status' in data and data['status'] not in ['available', 'pending', 'sold']:
            abort(400, description="Invalid status value")
        
        with get_db_cursor() as cursor:
            cursor.execute("""
                INSERT INTO pets (name, photo_urls, status)
                VALUES (%s, %s, %s)
                RETURNING id, name, photo_urls, status
            """, (
                data['name'],
                json.dumps(data['photoUrls']),
                data.get('status', 'available')
            ))
            
            pet = cursor.fetchone()
            
            # Convert to response format
            response = {
                'id': pet['id'],
                'name': pet['name'],
                'photoUrls': pet['photo_urls'],
                'status': pet['status']
            }
            
            return jsonify(response), 200
            
    except Exception as e:
        logger.error(f"Error adding pet: {e}")
        abort(400, description="Invalid input")

@app.route('/pet', methods=['PUT'])
def update_pet():
    """Update an existing pet."""
    try:
        data = request.get_json()
        
        # Validate required fields
        if not data or 'id' not in data:
            abort(400, description="Invalid input: id is required")
        
        # Validate status if provided
        if 'status' in data and data['status'] not in ['available', 'pending', 'sold']:
            abort(400, description="Invalid status value")
        
        with get_db_cursor() as cursor:
            # Check if pet exists
            cursor.execute("SELECT id FROM pets WHERE id = %s", (data['id'],))
            if not cursor.fetchone():
                abort(404, description="Pet not found")
            
            cursor.execute("""
                UPDATE pets 
                SET name = %s, 
                    photo_urls = %s, 
                    status = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                RETURNING id, name, photo_urls, status
            """, (
                data.get('name', ''),
                json.dumps(data.get('photoUrls', [])),
                data.get('status', 'available'),
                data['id']
            ))
            
            pet = cursor.fetchone()
            
            # Convert to response format
            response = {
                'id': pet['id'],
                'name': pet['name'],
                'photoUrls': pet['photo_urls'],
                'status': pet['status']
            }
            
            return jsonify(response), 200
            
    except Exception as e:
        logger.error(f"Error updating pet: {e}")
        abort(400, description="Invalid input")

@app.route('/pet/findByStatus', methods=['GET'])
def find_pets_by_status():
    """Finds Pets by status."""
    status = request.args.get('status')
    
    # Validate status
    if not status or status not in ['available', 'pending', 'sold']:
        abort(400, description="Invalid status value")
    
    try:
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT id, name, photo_urls, status
                FROM pets
                WHERE status = %s
            """, (status,))
            
            pets = cursor.fetchall()
            
            # Convert to response format
            response = []
            for pet in pets:
                response.append({
                    'id': pet['id'],
                    'name': pet['name'],
                    'photoUrls': pet['photo_urls'],
                    'status': pet['status']
                })
            
            return jsonify(response), 200
            
    except Exception as e:
        logger.error(f"Error finding pets by status: {e}")
        abort(500, description="Internal server error")

@app.route('/pet/<int:petId>', methods=['GET'])
def get_pet_by_id(petId):
    """Find pet by ID."""
    try:
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT id, name, photo_urls, status
                FROM pets
                WHERE id = %s
            """, (petId,))
            
            pet = cursor.fetchone()
            
            if not pet:
                abort(404, description="Pet not found")
            
            # Convert to response format
            response = {
                'id': pet['id'],
                'name': pet['name'],
                'photoUrls': pet['photo_urls'],
                'status': pet['status']
            }
            
            return jsonify(response), 200
            
    except Exception as e:
        logger.error(f"Error getting pet by ID: {e}")
        abort(500, description="Internal server error")

@app.route('/pet/<int:petId>', methods=['DELETE'])
def delete_pet(petId):
    """Deletes a pet."""
    try:
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM pets WHERE id = %s RETURNING id", (petId,))
            
            if not cursor.fetchone():
                abort(404, description="Pet not found")
            
            return '', 200
            
    except Exception as e:
        logger.error(f"Error deleting pet: {e}")
        abort(500, description="Internal server error")

# Store endpoints
@app.route('/store/order', methods=['POST'])
def place_order():
    """Place an order for a pet."""
    try:
        data = request.get_json()
        
        # Validate required fields
        if not data or 'petId' not in data:
            abort(400, description="Invalid input: petId is required")
        
        # Validate status if provided
        if 'status' in data and data['status'] not in ['placed', 'approved', 'delivered']:
            abort(400, description="Invalid status value")
        
        # Check if pet exists
        with get_db_cursor() as cursor:
            cursor.execute("SELECT id FROM pets WHERE id = %s", (data['petId'],))
            if not cursor.fetchone():
                abort(400, description="Pet not found")
            
            # Parse shipDate if provided
            ship_date = None
            if 'shipDate' in data and data['shipDate']:
                try:
                    ship_date = datetime.fromisoformat(data['shipDate'].replace('Z', '+00:00'))
                except ValueError:
                    abort(400, description="Invalid shipDate format")
            
            cursor.execute("""
                INSERT INTO orders (pet_id, quantity, ship_date, status, complete)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, pet_id, quantity, ship_date, status, complete
            """, (
                data['petId'],
                data.get('quantity', 1),
                ship_date,
                data.get('status', 'placed'),
                data.get('complete', False)
            ))
            
            order = cursor.fetchone()
            
            # Convert to response format
            response = {
                'id': order['id'],
                'petId': order['pet_id'],
                'quantity': order['quantity'],
                'shipDate': order['ship_date'].isoformat() if order['ship_date'] else None,
                'status': order['status'],
                'complete': order['complete']
            }
            
            return jsonify(response), 200
            
    except Exception as e:
        logger.error(f"Error placing order: {e}")
        abort(400, description="Invalid input")

@app.route('/store/order/<int:orderId>', methods=['GET'])
def get_order_by_id(orderId):
    """Find purchase order by ID."""
    try:
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT id, pet_id, quantity, ship_date, status, complete
                FROM orders
                WHERE id = %s
            """, (orderId,))
            
            order = cursor.fetchone()
            
            if not order:
                abort(404, description="Order not found")
            
            # Convert to response format
            response = {
                'id': order['id'],
                'petId': order['pet_id'],
                'quantity': order['quantity'],
                'shipDate': order['ship_date'].isoformat() if order['ship_date'] else None,
                'status': order['status'],
                'complete': order['complete']
            }
            
            return jsonify(response), 200
            
    except Exception as e:
        logger.error(f"Error getting order by ID: {e}")
        abort(500, description="Internal server error")

@app.route('/store/order/<int:orderId>', methods=['DELETE'])
def delete_order(orderId):
    """Delete purchase order by ID."""
    try:
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM orders WHERE id = %s RETURNING id", (orderId,))
            
            if not cursor.fetchone():
                abort(404, description="Order not found")
            
            return '', 200
            
    except Exception as e:
        logger.error(f"Error deleting order: {e}")
        abort(500, description="Internal server error")

# User endpoints
@app.route('/user', methods=['POST'])
def create_user():
    """Create user."""
    try:
        data = request.get_json()
        
        if not data:
            abort(400, description="Invalid input")
        
        with get_db_cursor() as cursor:
            cursor.execute("""
                INSERT INTO users (username, first_name, last_name, email, password, phone, user_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, username, first_name, last_name, email, password, phone, user_status
            """, (
                data.get('username', ''),
                data.get('firstName', ''),
                data.get('lastName', ''),
                data.get('email', ''),
                data.get('password', ''),
                data.get('phone', ''),
                data.get('userStatus', 0)
            ))
            
            user = cursor.fetchone()
            
            # Convert to response format
            response = {
                'id': user['id'],
                'username': user['username'],
                'firstName': user['first_name'],
                'lastName': user['last_name'],
                'email': user['email'],
                'password': user['password'],
                'phone': user['phone'],
                'userStatus': user['user_status']
            }
            
            return jsonify(response), 200
            
    except psycopg2.errors.UniqueViolation:
        abort(400, description="Username already exists")
    except Exception as e:
        logger.error(f"Error creating user: {e}")
        abort(400, description="Invalid input")

@app.route('/user/<username>', methods=['GET'])
def get_user_by_name(username):
    """Get user by user name."""
    try:
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT id, username, first_name, last_name, email, password, phone, user_status
                FROM users
                WHERE username = %s
            """, (username,))
            
            user = cursor.fetchone()
            
            if not user:
                abort(404, description="User not found")
            
            # Convert to response format
            response = {
                'id': user['id'],
                'username': user['username'],
                'firstName': user['first_name'],
                'lastName': user['last_name'],
                'email': user['email'],
                'password': user['password'],
                'phone': user['phone'],
                'userStatus': user['user_status']
            }
            
            return jsonify(response), 200
            
    except Exception as e:
        logger.error(f"Error getting user by name: {e}")
        abort(500, description="Internal server error")

@app.route('/user/<username>', methods=['PUT'])
def update_user(username):
    """Updated user."""
    try:
        data = request.get_json()
        
        if not data:
            abort(400, description="Invalid input")
        
        with get_db_cursor() as cursor:
            # Check if user exists
            cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
            if not cursor.fetchone():
                abort(404, description="User not found")
            
            cursor.execute("""
                UPDATE users 
                SET username = %s,
                    first_name = %s,
                    last_name = %s,
                    email = %s,
                    password = %s,
                    phone = %s,
                    user_status = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE username = %s
                RETURNING id, username, first_name, last_name, email, password, phone, user_status
            """, (
                data.get('username', username),
                data.get('firstName', ''),
                data.get('lastName', ''),
                data.get('email', ''),
                data.get('password', ''),
                data.get('phone', ''),
                data.get('userStatus', 0),
                username
            ))
            
            user = cursor.fetchone()
            
            # Convert to response format
            response = {
                'id': user['id'],
                'username': user['username'],
                'firstName': user['first_name'],
                'lastName': user['last_name'],
                'email': user['email'],
                'password': user['password'],
                'phone': user['phone'],
                'userStatus': user['user_status']
            }
            
            return jsonify(response), 200
            
    except psycopg2.errors.UniqueViolation:
        abort(400, description="Username already exists")
    except Exception as e:
        logger.error(f"Error updating user: {e}")
        abort(400, description="Invalid input")

@app.route('/user/<username>', methods=['DELETE'])
def delete_user(username):
    """Delete user."""
    try:
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM users WHERE username = %s RETURNING id", (username,))
            
            if not cursor.fetchone():
                abort(404, description="User not found")
            
            return '', 200
            
    except Exception as e:
        logger.error(f"Error deleting user: {e}")
        abort(500, description="Internal server error")

@app.route('/user/login', methods=['GET'])
def login_user():
    """Logs user into the system."""
    username = request.args.get('username')
    password = request.args.get('password')
    
    if not username or not password:
        abort(400, description="Invalid credentials")
    
    try:
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT id, username, password
                FROM users
                WHERE username = %s AND password = %s
            """, (username, password))
            
            user = cursor.fetchone()
            
            if not user:
                abort(400, description="Invalid credentials")
            
            # Return a simple success message as per OpenAPI spec
            return jsonify("Logged in successfully"), 200
            
    except Exception as e:
        logger.error(f"Error logging in user: {e}")
        abort(400, description="Invalid credentials")

# Error handlers
@app.errorhandler(400)
def bad_request(error):
    return jsonify({'error': error.description}), 400

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': error.description}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'Internal server error'}), 500

# Main entry point
if __name__ == '__main__':
    # Initialize before running
    try:
        init_connection_pool()
        init_database()
    except Exception as e:
        logger.error(f"Initialization failed: {e}")
    
    # Get port from environment variable or use default
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)