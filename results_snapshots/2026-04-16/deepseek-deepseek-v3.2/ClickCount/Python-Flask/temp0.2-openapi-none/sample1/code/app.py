import os
import uuid
from datetime import datetime
from flask import Flask, request, jsonify
import psycopg2
from psycopg2.extras import RealDictCursor
import logging

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database connection parameters from environment variables
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_USER = os.getenv('DB_USER', 'postgres')
DB_PASSWORD = os.getenv('DB_PASSWORD', '')
DB_NAME = os.getenv('DB_NAME', 'clickdb')

# Application port
PORT = int(os.getenv('PORT', 5001))

def get_db_connection():
    """Create and return a database connection."""
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        return conn
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        raise

def init_database():
    """Initialize database tables if they don't exist."""
    # Use a lock file to prevent multiple workers from running initialization concurrently
    lock_file = '/tmp/db_init.lock'
    
    try:
        # Try to create lock file exclusively
        fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        
        logger.info("Initializing database tables...")
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Create clicks table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS clicks (
                id VARCHAR(36) PRIMARY KEY,
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create index on timestamp for faster queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_clicks_timestamp ON clicks(timestamp)
        """)
        
        conn.commit()
        cursor.close()
        conn.close()
        logger.info("Database tables initialized successfully")
        
        # Clean up lock file
        os.unlink(lock_file)
        
    except FileExistsError:
        # Another worker is already initializing the database
        logger.info("Database initialization already in progress by another worker")
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
        # Clean up lock file on error
        try:
            os.unlink(lock_file)
        except:
            pass

@app.route('/click', methods=['POST'])
def register_click():
    """Register a new click."""
    try:
        # Generate unique ID for the click
        click_id = str(uuid.uuid4())
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Insert click with current timestamp
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
        
        # Validate parameters
        if not date_str or not direction:
            return jsonify({'error': 'Missing required parameters: date and direction'}), 400
        
        if direction not in ['<', '>', '<=', '>=']:
            return jsonify({'error': 'Invalid direction parameter. Must be one of: <, >, <=, >='}), 400
        
        # Parse and validate date
        try:
            # Parse date string (format: YYYY-MM-DD)
            filter_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
        
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Build query based on direction
        if direction in ['<', '<=']:
            # For < and <=, we want clicks before the date
            # Convert date to timestamp at start of day for comparison
            query = f"""
                SELECT id, timestamp 
                FROM clicks 
                WHERE timestamp {direction} %s::timestamp
                ORDER BY timestamp DESC
            """
            param_date = datetime.combine(filter_date, datetime.min.time())
        else:
            # For > and >=, we want clicks after the date
            # Convert date to timestamp at end of day for comparison
            query = f"""
                SELECT id, timestamp 
                FROM clicks 
                WHERE timestamp {direction} %s::timestamp
                ORDER BY timestamp ASC
            """
            param_date = datetime.combine(filter_date, datetime.max.time())
        
        cursor.execute(query, (param_date,))
        clicks = cursor.fetchall()
        
        cursor.close()
        conn.close()
        
        if not clicks:
            return jsonify({'error': 'No clicks found'}), 404
        
        # Format response
        result = []
        for click in clicks:
            result.append({
                'id': click['id'],
                'timestamp': click['timestamp'].isoformat() if click['timestamp'] else None
            })
        
        return jsonify(result), 200
        
    except Exception as e:
        logger.error(f"Error retrieving clicks: {e}")
        return jsonify({'error': 'Invalid request'}), 400

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    # Initialize database on startup
    init_database()
    
    # Start Flask app
    app.run(host='0.0.0.0', port=PORT)
else:
    # When running with gunicorn, initialize database
    # This will be called in each worker process, but the lock file prevents concurrent initialization
    init_database()