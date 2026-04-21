import os
import uuid
from datetime import datetime
from flask import Flask, request, jsonify
import psycopg2
from psycopg2.extras import RealDictCursor
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Database connection function
def get_db_connection():
    conn = psycopg2.connect(
        host=os.environ.get('DB_HOST', 'localhost'),
        port=os.environ.get('DB_PORT', '5432'),
        user=os.environ.get('DB_USER', 'postgres'),
        password=os.environ.get('DB_PASSWORD', ''),
        database=os.environ.get('DB_NAME', 'clickdb')
    )
    return conn

# Initialize database tables
def init_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Create clicks table if it doesn't exist
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS clicks (
                id VARCHAR(255) PRIMARY KEY,
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        cursor.close()
        conn.close()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        raise

# Initialize database when app starts
init_db()

@app.route('/click', methods=['POST'])
def register_click():
    """Register a click in the database."""
    try:
        # Generate unique ID for the click
        click_id = str(uuid.uuid4())
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Insert click into database
        cursor.execute(
            "INSERT INTO clicks (id, timestamp) VALUES (%s, CURRENT_TIMESTAMP)",
            (click_id,)
        )
        
        conn.commit()
        cursor.close()
        conn.close()
        
        logger.info(f"Click registered with ID: {click_id}")
        return '', 201
        
    except Exception as e:
        logger.error(f"Error registering click: {e}")
        return jsonify({'error': 'Invalid request'}), 400

@app.route('/clicks', methods=['GET'])
def get_clicks():
    """Retrieve clicks filtered by date and direction."""
    try:
        # Get query parameters
        date_str = request.args.get('date')
        direction = request.args.get('direction')
        
        # Validate required parameters
        if not date_str or not direction:
            return jsonify({'error': 'Missing required parameters: date and direction'}), 400
        
        # Validate direction parameter
        valid_directions = ['<', '>', '<=', '>=']
        if direction not in valid_directions:
            return jsonify({'error': f'Invalid direction. Must be one of: {valid_directions}'}), 400
        
        # Parse date
        try:
            # Parse date string to datetime object
            filter_date = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
        
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Build query based on direction
        if direction == '<':
            query = "SELECT id, timestamp FROM clicks WHERE timestamp < %s ORDER BY timestamp"
        elif direction == '>':
            query = "SELECT id, timestamp FROM clicks WHERE timestamp > %s ORDER BY timestamp"
        elif direction == '<=':
            query = "SELECT id, timestamp FROM clicks WHERE timestamp <= %s ORDER BY timestamp"
        elif direction == '>=':
            query = "SELECT id, timestamp FROM clicks WHERE timestamp >= %s ORDER BY timestamp"
        
        # Execute query
        cursor.execute(query, (filter_date,))
        clicks = cursor.fetchall()
        
        cursor.close()
        conn.close()
        
        # Format response
        formatted_clicks = []
        for click in clicks:
            formatted_clicks.append({
                'id': click['id'],
                'timestamp': click['timestamp'].isoformat() if click['timestamp'] else None
            })
        
        if not formatted_clicks:
            return jsonify({'error': 'No clicks found'}), 404
        
        return jsonify(formatted_clicks), 200
        
    except Exception as e:
        logger.error(f"Error retrieving clicks: {e}")
        return jsonify({'error': 'Invalid request'}), 400

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)