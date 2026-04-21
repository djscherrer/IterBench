import os
import json
from datetime import datetime
from flask import Flask, request, jsonify
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

# Database connection parameters from environment variables
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_USER = os.getenv('DB_USER', 'postgres')
DB_PASSWORD = os.getenv('DB_PASSWORD', 'postgres')
DB_NAME = os.getenv('DB_NAME', 'petstore')

def get_db_connection():
    """Create and return a database connection."""
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME
    )
    return conn

def init_db():
    """Initialize database tables if they don't exist."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Create pets table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pets (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            photo_urls TEXT[],
            status VARCHAR(50) CHECK (status IN ('available', 'pending', 'sold'))
        )
    ''')
    
    # Create orders table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            pet_id INTEGER,
            quantity INTEGER,
            ship_date TIMESTAMP,
            status VARCHAR(50) CHECK (status IN ('placed', 'approved', 'delivered')),
            complete BOOLEAN
        )
    ''')
    
    # Create users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(255) UNIQUE NOT NULL,
            first_name VARCHAR(255),
            last_name VARCHAR(255),
            email VARCHAR(255),
            password VARCHAR(255),
            phone VARCHAR(50),
            user_status INTEGER
        )
    ''')
    
    conn.commit()
    cursor.close()
    conn.close()

# Initialize database when app starts
init_db()

# Helper function to convert database rows to dictionaries
def row_to_dict(row):
    """Convert a database row (RealDictRow) to a regular dictionary."""
    return dict(row)

# Pet endpoints
@app.route('/pet', methods=['POST'])
def add_pet():
    """Add a new pet to the store."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Invalid input'}), 400
        
        name = data.get('name')
        photo_urls = data.get('photoUrls', [])
        status = data.get('status')
        
        if not name or not isinstance(photo_urls, list):
            return jsonify({'error': 'Invalid input'}), 400
        
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute(
            'INSERT INTO pets (name, photo_urls, status) VALUES (%s, %s, %s) RETURNING *',
            (name, photo_urls, status)
        )
        pet = cursor.fetchone()
        
        conn.commit()
        cursor.close()
        conn.close()
        
        # Convert to expected response format
        pet_dict = row_to_dict(pet)
        pet_dict['photoUrls'] = pet_dict.pop('photo_urls', [])
        
        return jsonify(pet_dict), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/pet', methods=['PUT'])
def update_pet():
    """Update an existing pet."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Invalid input'}), 400
        
        pet_id = data.get('id')
        name = data.get('name')
        photo_urls = data.get('photoUrls', [])
        status = data.get('status')
        
        if not pet_id or not name or not isinstance(photo_urls, list):
            return jsonify({'error': 'Invalid input'}), 400
        
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Check if pet exists
        cursor.execute('SELECT * FROM pets WHERE id = %s', (pet_id,))
        existing_pet = cursor.fetchone()
        
        if not existing_pet:
            cursor.close()
            conn.close()
            return jsonify({'error': 'Pet not found'}), 404
        
        # Update pet
        cursor.execute(
            'UPDATE pets SET name = %s, photo_urls = %s, status = %s WHERE id = %s RETURNING *',
            (name, photo_urls, status, pet_id)
        )
        pet = cursor.fetchone()
        
        conn.commit()
        cursor.close()
        conn.close()
        
        # Convert to expected response format
        pet_dict = row_to_dict(pet)
        pet_dict['photoUrls'] = pet_dict.pop('photo_urls', [])
        
        return jsonify(pet_dict), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/pet/findByStatus', methods=['GET'])
def find_pets_by_status():
    """Finds Pets by status."""
    status = request.args.get('status')
    
    if not status or status not in ['available', 'pending', 'sold']:
        return jsonify({'error': 'Invalid status value'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    cursor.execute('SELECT * FROM pets WHERE status = %s', (status,))
    pets = cursor.fetchall()
    
    cursor.close()
    conn.close()
    
    # Convert to expected response format
    pets_list = []
    for pet in pets:
        pet_dict = row_to_dict(pet)
        pet_dict['photoUrls'] = pet_dict.pop('photo_urls', [])
        pets_list.append(pet_dict)
    
    return jsonify(pets_list), 200

@app.route('/pet/<int:pet_id>', methods=['GET'])
def get_pet_by_id(pet_id):
    """Find pet by ID."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    cursor.execute('SELECT * FROM pets WHERE id = %s', (pet_id,))
    pet = cursor.fetchone()
    
    cursor.close()
    conn.close()
    
    if not pet:
        return jsonify({'error': 'Pet not found'}), 404
    
    # Convert to expected response format
    pet_dict = row_to_dict(pet)
    pet_dict['photoUrls'] = pet_dict.pop('photo_urls', [])
    
    return jsonify(pet_dict), 200

@app.route('/pet/<int:pet_id>', methods=['DELETE'])
def delete_pet(pet_id):
    """Deletes a pet."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Check if pet exists
    cursor.execute('SELECT id FROM pets WHERE id = %s', (pet_id,))
    existing_pet = cursor.fetchone()
    
    if not existing_pet:
        cursor.close()
        conn.close()
        return jsonify({'error': 'Pet not found'}), 404
    
    cursor.execute('DELETE FROM pets WHERE id = %s', (pet_id,))
    
    conn.commit()
    cursor.close()
    conn.close()
    
    return jsonify({'message': 'Pet deleted successfully'}), 200

# Store endpoints
@app.route('/store/order', methods=['POST'])
def place_order():
    """Place an order for a pet."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Invalid input'}), 400
        
        pet_id = data.get('petId')
        quantity = data.get('quantity')
        ship_date = data.get('shipDate')
        status = data.get('status')
        complete = data.get('complete', False)
        
        # Parse ship_date if provided
        ship_date_parsed = None
        if ship_date:
            try:
                ship_date_parsed = datetime.fromisoformat(ship_date.replace('Z', '+00:00'))
            except ValueError:
                return jsonify({'error': 'Invalid date format'}), 400
        
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute(
            '''INSERT INTO orders (pet_id, quantity, ship_date, status, complete) 
               VALUES (%s, %s, %s, %s, %s) RETURNING *''',
            (pet_id, quantity, ship_date_parsed, status, complete)
        )
        order = cursor.fetchone()
        
        conn.commit()
        cursor.close()
        conn.close()
        
        # Convert to expected response format
        order_dict = row_to_dict(order)
        order_dict['petId'] = order_dict.pop('pet_id')
        order_dict['shipDate'] = order_dict.pop('ship_date')
        
        # Format shipDate as ISO string
        if order_dict['shipDate']:
            order_dict['shipDate'] = order_dict['shipDate'].isoformat()
        
        return jsonify(order_dict), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/store/order/<int:order_id>', methods=['GET'])
def get_order_by_id(order_id):
    """Find purchase order by ID."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    cursor.execute('SELECT * FROM orders WHERE id = %s', (order_id,))
    order = cursor.fetchone()
    
    cursor.close()
    conn.close()
    
    if not order:
        return jsonify({'error': 'Order not found'}), 404
    
    # Convert to expected response format
    order_dict = row_to_dict(order)
    order_dict['petId'] = order_dict.pop('pet_id')
    order_dict['shipDate'] = order_dict.pop('ship_date')
    
    # Format shipDate as ISO string
    if order_dict['shipDate']:
        order_dict['shipDate'] = order_dict['shipDate'].isoformat()
    
    return jsonify(order_dict), 200

@app.route('/store/order/<int:order_id>', methods=['DELETE'])
def delete_order(order_id):
    """Delete purchase order by ID."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Check if order exists
    cursor.execute('SELECT id FROM orders WHERE id = %s', (order_id,))
    existing_order = cursor.fetchone()
    
    if not existing_order:
        cursor.close()
        conn.close()
        return jsonify({'error': 'Order not found'}), 404
    
    cursor.execute('DELETE FROM orders WHERE id = %s', (order_id,))
    
    conn.commit()
    cursor.close()
    conn.close()
    
    return jsonify({'message': 'Order deleted successfully'}), 200

# User endpoints
@app.route('/user', methods=['POST'])
def create_user():
    """Create user."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Invalid input'}), 400
        
        username = data.get('username')
        first_name = data.get('firstName')
        last_name = data.get('lastName')
        email = data.get('email')
        password = data.get('password')
        phone = data.get('phone')
        user_status = data.get('userStatus')
        
        if not username:
            return jsonify({'error': 'Invalid input'}), 400
        
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        try:
            cursor.execute(
                '''INSERT INTO users (username, first_name, last_name, email, password, phone, user_status) 
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *''',
                (username, first_name, last_name, email, password, phone, user_status)
            )
            user = cursor.fetchone()
            
            conn.commit()
        except psycopg2.IntegrityError:
            cursor.close()
            conn.close()
            return jsonify({'error': 'Username already exists'}), 400
        
        cursor.close()
        conn.close()
        
        # Convert to expected response format
        user_dict = row_to_dict(user)
        user_dict['firstName'] = user_dict.pop('first_name')
        user_dict['lastName'] = user_dict.pop('last_name')
        user_dict['userStatus'] = user_dict.pop('user_status')
        
        return jsonify(user_dict), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/user/<username>', methods=['GET'])
def get_user_by_name(username):
    """Get user by user name."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    cursor.execute('SELECT * FROM users WHERE username = %s', (username,))
    user = cursor.fetchone()
    
    cursor.close()
    conn.close()
    
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    # Convert to expected response format
    user_dict = row_to_dict(user)
    user_dict['firstName'] = user_dict.pop('first_name')
    user_dict['lastName'] = user_dict.pop('last_name')
    user_dict['userStatus'] = user_dict.pop('user_status')
    
    return jsonify(user_dict), 200

@app.route('/user/<username>', methods=['PUT'])
def update_user(username):
    """Updated user."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Invalid input'}), 400
        
        first_name = data.get('firstName')
        last_name = data.get('lastName')
        email = data.get('email')
        password = data.get('password')
        phone = data.get('phone')
        user_status = data.get('userStatus')
        
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Check if user exists
        cursor.execute('SELECT * FROM users WHERE username = %s', (username,))
        existing_user = cursor.fetchone()
        
        if not existing_user:
            cursor.close()
            conn.close()
            return jsonify({'error': 'User not found'}), 404
        
        # Update user
        cursor.execute(
            '''UPDATE users SET first_name = %s, last_name = %s, email = %s, 
               password = %s, phone = %s, user_status = %s WHERE username = %s RETURNING *''',
            (first_name, last_name, email, password, phone, user_status, username)
        )
        user = cursor.fetchone()
        
        conn.commit()
        cursor.close()
        conn.close()
        
        # Convert to expected response format
        user_dict = row_to_dict(user)
        user_dict['firstName'] = user_dict.pop('first_name')
        user_dict['lastName'] = user_dict.pop('last_name')
        user_dict['userStatus'] = user_dict.pop('user_status')
        
        return jsonify(user_dict), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/user/<username>', methods=['DELETE'])
def delete_user(username):
    """Delete user."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Check if user exists
    cursor.execute('SELECT id FROM users WHERE username = %s', (username,))
    existing_user = cursor.fetchone()
    
    if not existing_user:
        cursor.close()
        conn.close()
        return jsonify({'error': 'User not found'}), 404
    
    cursor.execute('DELETE FROM users WHERE username = %s', (username,))
    
    conn.commit()
    cursor.close()
    conn.close()
    
    return jsonify({'message': 'User deleted successfully'}), 200

@app.route('/user/login', methods=['GET'])
def login_user():
    """Logs user into the system."""
    username = request.args.get('username')
    password = request.args.get('password')
    
    if not username or not password:
        return jsonify({'error': 'Invalid credentials'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    cursor.execute('SELECT * FROM users WHERE username = %s AND password = %s', (username, password))
    user = cursor.fetchone()
    
    cursor.close()
    conn.close()
    
    if not user:
        return jsonify({'error': 'Invalid credentials'}), 400
    
    # Return a simple success message as per OpenAPI spec
    return jsonify("Logged in successfully"), 200

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5001))
    app.run(host='0.0.0.0', port=port)