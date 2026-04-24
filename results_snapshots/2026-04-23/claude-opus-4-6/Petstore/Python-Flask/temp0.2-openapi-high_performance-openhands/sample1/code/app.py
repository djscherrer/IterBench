import os
import threading
import psycopg2
import psycopg2.pool
from flask import Flask, request, jsonify

app = Flask(__name__)

DB_CONFIG = {
    'host': os.environ.get('DB_HOST', 'localhost'),
    'port': int(os.environ.get('DB_PORT', 5432)),
    'user': os.environ.get('DB_USER', 'postgres'),
    'password': os.environ.get('DB_PASSWORD', 'postgres'),
    'database': os.environ.get('DB_NAME', 'testdb'),
}

_pool = None
_pool_pid = None
_pool_lock = threading.Lock()
_db_initialized = False
_init_lock = threading.Lock()

def get_pool():
    global _pool, _pool_pid
    pid = os.getpid()
    if _pool is None or _pool_pid != pid:
        with _pool_lock:
            if _pool is None or _pool_pid != pid:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=2,
                    maxconn=20,
                    **DB_CONFIG
                )
                _pool_pid = pid
    return _pool

def get_conn():
    return get_pool().getconn()

def put_conn(conn):
    get_pool().putconn(conn)

def init_db():
    global _db_initialized
    if _db_initialized:
        return
    with _init_lock:
        if _db_initialized:
            return
        conn = get_conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pets (
                    id BIGSERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    photo_urls TEXT[] NOT NULL DEFAULT '{}',
                    status VARCHAR(20) DEFAULT 'available'
                );
                CREATE INDEX IF NOT EXISTS idx_pets_status ON pets(status);
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id BIGSERIAL PRIMARY KEY,
                    pet_id BIGINT,
                    quantity INTEGER DEFAULT 0,
                    ship_date TIMESTAMPTZ,
                    status VARCHAR(20) DEFAULT 'placed',
                    complete BOOLEAN DEFAULT FALSE
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    username VARCHAR(255) UNIQUE NOT NULL,
                    first_name VARCHAR(255) DEFAULT '',
                    last_name VARCHAR(255) DEFAULT '',
                    email VARCHAR(255) DEFAULT '',
                    password VARCHAR(255) DEFAULT '',
                    phone VARCHAR(255) DEFAULT '',
                    user_status INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
            """)
            cur.close()
            conn.autocommit = False
            _db_initialized = True
        finally:
            put_conn(conn)

try:
    init_db()
except Exception:
    pass

@app.before_request
def ensure_db():
    if not _db_initialized:
        init_db()

# ---- Pet endpoints ----

@app.route('/pet', methods=['POST'])
def add_pet():
    data = request.get_json()
    if not data or 'name' not in data or 'photoUrls' not in data:
        return jsonify({"error": "Invalid input"}), 400
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO pets (name, photo_urls, status) VALUES (%s, %s, %s) RETURNING id",
            (data['name'], data.get('photoUrls', []), data.get('status', 'available'))
        )
        pet_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        result = {
            'id': pet_id,
            'name': data['name'],
            'photoUrls': data.get('photoUrls', []),
            'status': data.get('status', 'available')
        }
        return jsonify(result), 200
    except Exception:
        conn.rollback()
        return jsonify({"error": "Invalid input"}), 400
    finally:
        put_conn(conn)

@app.route('/pet', methods=['PUT'])
def update_pet():
    data = request.get_json()
    if not data or 'id' not in data:
        return jsonify({"error": "Invalid input"}), 400
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE pets SET name = %s, photo_urls = %s, status = %s
               WHERE id = %s RETURNING id""",
            (data.get('name', ''), data.get('photoUrls', []),
             data.get('status', 'available'), data['id'])
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        if row is None:
            return jsonify({"error": "Pet not found"}), 404
        result = {
            'id': data['id'],
            'name': data.get('name', ''),
            'photoUrls': data.get('photoUrls', []),
            'status': data.get('status', 'available')
        }
        return jsonify(result), 200
    except Exception:
        conn.rollback()
        return jsonify({"error": "Invalid input"}), 400
    finally:
        put_conn(conn)

@app.route('/pet/findByStatus', methods=['GET'])
def find_pets_by_status():
    status = request.args.get('status')
    if status not in ('available', 'pending', 'sold'):
        return jsonify([]), 200
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, name, photo_urls, status FROM pets WHERE status = %s",
            (status,)
        )
        rows = cur.fetchall()
        cur.close()
        pets = [{'id': r[0], 'name': r[1], 'photoUrls': list(r[2]) if r[2] else [], 'status': r[3]} for r in rows]
        return jsonify(pets), 200
    finally:
        put_conn(conn)

@app.route('/pet/<int:petId>', methods=['GET'])
def get_pet_by_id(petId):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name, photo_urls, status FROM pets WHERE id = %s", (petId,))
        row = cur.fetchone()
        cur.close()
        if row is None:
            return jsonify({"error": "Pet not found"}), 404
        return jsonify({'id': row[0], 'name': row[1], 'photoUrls': list(row[2]) if row[2] else [], 'status': row[3]}), 200
    finally:
        put_conn(conn)

@app.route('/pet/<int:petId>', methods=['DELETE'])
def delete_pet(petId):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM pets WHERE id = %s RETURNING id", (petId,))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        if row is None:
            return jsonify({"error": "Pet not found"}), 404
        return jsonify({"message": "successful operation"}), 200
    except Exception:
        conn.rollback()
        return jsonify({"error": "Error"}), 400
    finally:
        put_conn(conn)

# ---- Store/Order endpoints ----

@app.route('/store/order', methods=['POST'])
def place_order():
    data = request.get_json()
    if not data:
        data = {}
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO orders (pet_id, quantity, ship_date, status, complete)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (data.get('petId'), data.get('quantity', 0),
             data.get('shipDate'), data.get('status', 'placed'),
             data.get('complete', False))
        )
        order_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        result = {
            'id': order_id,
            'petId': data.get('petId'),
            'quantity': data.get('quantity', 0),
            'shipDate': data.get('shipDate'),
            'status': data.get('status', 'placed'),
            'complete': data.get('complete', False)
        }
        return jsonify(result), 200
    except Exception:
        conn.rollback()
        return jsonify({"error": "Invalid input"}), 400
    finally:
        put_conn(conn)

@app.route('/store/order/<int:orderId>', methods=['GET'])
def get_order_by_id(orderId):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id = %s",
            (orderId,)
        )
        row = cur.fetchone()
        cur.close()
        if row is None:
            return jsonify({"error": "Order not found"}), 404
        return jsonify({
            'id': row[0], 'petId': row[1], 'quantity': row[2],
            'shipDate': row[3].isoformat() if row[3] else None,
            'status': row[4], 'complete': row[5]
        }), 200
    finally:
        put_conn(conn)

@app.route('/store/order/<int:orderId>', methods=['DELETE'])
def delete_order(orderId):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM orders WHERE id = %s RETURNING id", (orderId,))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        if row is None:
            return jsonify({"error": "Order not found"}), 404
        return jsonify({"message": "successful operation"}), 200
    except Exception:
        conn.rollback()
        return jsonify({"error": "Error"}), 400
    finally:
        put_conn(conn)

# ---- User endpoints ----

@app.route('/user', methods=['POST'])
def create_user():
    data = request.get_json()
    if not data:
        data = {}
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO users (username, first_name, last_name, email, password, phone, user_status)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (data.get('username', ''), data.get('firstName', ''),
             data.get('lastName', ''), data.get('email', ''),
             data.get('password', ''), data.get('phone', ''),
             data.get('userStatus', 0))
        )
        user_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        result = {
            'id': user_id,
            'username': data.get('username', ''),
            'firstName': data.get('firstName', ''),
            'lastName': data.get('lastName', ''),
            'email': data.get('email', ''),
            'password': data.get('password', ''),
            'phone': data.get('phone', ''),
            'userStatus': data.get('userStatus', 0)
        }
        return jsonify(result), 200
    except Exception:
        conn.rollback()
        return jsonify({"error": "Invalid input"}), 400
    finally:
        put_conn(conn)

@app.route('/user/<username>', methods=['GET'])
def get_user_by_name(username):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username = %s",
            (username,)
        )
        row = cur.fetchone()
        cur.close()
        if row is None:
            return jsonify({"error": "User not found"}), 404
        return jsonify({
            'id': row[0], 'username': row[1], 'firstName': row[2],
            'lastName': row[3], 'email': row[4], 'password': row[5],
            'phone': row[6], 'userStatus': row[7]
        }), 200
    finally:
        put_conn(conn)

@app.route('/user/<username>', methods=['PUT'])
def update_user(username):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid input"}), 400
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE users SET username = %s, first_name = %s, last_name = %s,
               email = %s, password = %s, phone = %s, user_status = %s
               WHERE username = %s RETURNING id""",
            (data.get('username', username), data.get('firstName', ''),
             data.get('lastName', ''), data.get('email', ''),
             data.get('password', ''), data.get('phone', ''),
             data.get('userStatus', 0), username)
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        if row is None:
            return jsonify({"error": "User not found"}), 404
        result = {
            'id': row[0],
            'username': data.get('username', username),
            'firstName': data.get('firstName', ''),
            'lastName': data.get('lastName', ''),
            'email': data.get('email', ''),
            'password': data.get('password', ''),
            'phone': data.get('phone', ''),
            'userStatus': data.get('userStatus', 0)
        }
        return jsonify(result), 200
    except Exception:
        conn.rollback()
        return jsonify({"error": "Invalid input"}), 400
    finally:
        put_conn(conn)

@app.route('/user/<username>', methods=['DELETE'])
def delete_user(username):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE username = %s RETURNING id", (username,))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        if row is None:
            return jsonify({"error": "User not found"}), 404
        return jsonify({"message": "successful operation"}), 200
    except Exception:
        conn.rollback()
        return jsonify({"error": "Error"}), 400
    finally:
        put_conn(conn)

@app.route('/user/login', methods=['GET'])
def login_user():
    username = request.args.get('username')
    password = request.args.get('password')
    if not username or not password:
        return jsonify({"error": "Invalid credentials"}), 400
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT password FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
        cur.close()
        if row is None or row[0] != password:
            return jsonify({"error": "Invalid credentials"}), 400
        return jsonify("Logged in"), 200
    finally:
        put_conn(conn)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)
