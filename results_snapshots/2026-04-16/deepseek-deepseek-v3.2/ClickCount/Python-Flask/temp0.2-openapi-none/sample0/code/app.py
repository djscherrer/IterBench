import os
import uuid
from datetime import datetime
from flask import Flask, request, jsonify
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import sql

app = Flask(__name__)

# Database connection parameters from environment variables
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_USER = os.getenv('DB_USER', 'postgres')
DB_PASSWORD = os.getenv('DB_PASSWORD', '')
DB_NAME = os.getenv('DB_NAME', 'clickdb')

def get_db_connection():
    """Create and return a database connection."""
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname=DB_NAME,
        cursor_factory=RealDictCursor
    )

def init_db():
    """Initialize the database by creating the clicks table if it doesn't exist."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Create clicks table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS clicks (
            id UUID PRIMARY KEY,
            timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    conn.commit()
    cur.close()
    conn.close()

# Initialize database when the app starts
# This is safe for gunicorn with --preload flag as it runs before worker processes fork
init_db()

@app.route('/click', methods=['POST'])
def register_click():
    """Register a click in the database."""
    try:
        click_id = str(uuid.uuid4())
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Insert the click with current timestamp
        cur.execute(
            "INSERT INTO clicks (id, timestamp) VALUES (%s, CURRENT_TIMESTAMP)",
            (click_id,)
        )
        
        conn.commit()
        
        # Get the inserted click to return the timestamp
        cur.execute("SELECT id, timestamp FROM clicks WHERE id = %s", (click_id,))
        click = cur.fetchone()
        
        cur.close()
        conn.close()
        
        # Convert timestamp to ISO format string
        click_data = {
            'id': click['id'],
            'timestamp': click['timestamp'].isoformat()
        }
        
        return jsonify(click_data), 201
        
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/clicks', methods=['GET'])
def get_clicks():
    """Retrieve clicks filtered by date and direction."""
    try:
        date_str = request.args.get('date')
        direction = request.args.get('direction')
        
        if not date_str or not direction:
            return jsonify({'error': 'Missing required parameters: date and direction'}), 400
        
        # Validate date format
        try:
            filter_date = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        except ValueError:
            return jsonify({'error': 'Invalid date format. Use ISO format (YYYY-MM-DD)'}), 400
        
        # Validate direction
        valid_directions = ['<', '>', '<=', '>=']
        if direction not in valid_directions:
            return jsonify({'error': f'Invalid direction. Must be one of {valid_directions}'}), 400
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Build the query with parameterized SQL to prevent injection
        query = sql.SQL("SELECT id, timestamp FROM clicks WHERE timestamp {} %s ORDER BY timestamp").format(
            sql.SQL(direction)
        )
        
        cur.execute(query, (filter_date,))
        clicks = cur.fetchall()
        
        cur.close()
        conn.close()
        
        if not clicks:
            return jsonify({'error': 'No clicks found'}), 404
        
        # Convert timestamps to ISO format strings
        result = []
        for click in clicks:
            result.append({
                'id': click['id'],
                'timestamp': click['timestamp'].isoformat()
            })
        
        return jsonify(result), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 400

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5001))
    app.run(host='0.0.0.0', port=port)