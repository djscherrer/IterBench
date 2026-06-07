import os
import json
import threading
from flask import Flask, request, jsonify, Response
import psycopg2
from psycopg2 import pool, errors
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

DB_HOST = os.environ.get('DB_HOST', 'localhost')
DB_PORT = os.environ.get('DB_PORT', '5432')
DB_USER = os.environ.get('DB_USER', 'postgres')
DB_PASSWORD = os.environ.get('DB_PASSWORD', 'postgres')
DB_NAME = os.environ.get('DB_NAME', 'testdb')

_db_pool = None
_pool_lock = threading.Lock()


def get_pool():
    global _db_pool
    if _db_pool is None:
        with _pool_lock:
            if _db_pool is None:
                _db_pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=20,
                    host=DB_HOST,
                    port=DB_PORT,
                    user=DB_USER,
                    password=DB_PASSWORD,
                    dbname=DB_NAME,
                )
    return _db_pool


class DBConn:
    def __init__(self):
        self.conn = None

    def __enter__(self):
        self.conn = get_pool().getconn()
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn is not None:
            get_pool().putconn(self.conn)


def init_db():
    # Retry on connection failures
    import time
    last_err = None
    for _ in range(10):
        try:
            conn = psycopg2.connect(
                host=DB_HOST, port=DB_PORT, user=DB_USER,
                password=DB_PASSWORD, dbname=DB_NAME
            )
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS pets (
                        id BIGSERIAL PRIMARY KEY,
                        name TEXT NOT NULL,
                        photo_urls JSONB NOT NULL DEFAULT '[]'::jsonb,
                        status TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_pets_status ON pets(status);

                    CREATE TABLE IF NOT EXISTS orders (
                        id BIGSERIAL PRIMARY KEY,
                        pet_id BIGINT,
                        quantity INTEGER,
                        ship_date TIMESTAMPTZ,
                        status TEXT,
                        complete BOOLEAN
                    );

                    CREATE TABLE IF NOT EXISTS users (
                        id BIGSERIAL PRIMARY KEY,
                        username TEXT UNIQUE,
                        first_name TEXT,
                        last_name TEXT,
                        email TEXT,
                        password TEXT,
                        phone TEXT,
                        user_status INTEGER
                    );
                    CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
                """)
            conn.close()
            return
        except Exception as e:
            last_err = e
            time.sleep(1)
    raise last_err


# Initialize DB at import time (preload-safe; idempotent CREATE IF NOT EXISTS)
init_db()


def pet_row_to_dict(row):
    if row is None:
        return None
    photo_urls = row['photo_urls']
    if isinstance(photo_urls, str):
        try:
            photo_urls = json.loads(photo_urls)
        except Exception:
            photo_urls = []
    d = {
        'id': row['id'],
        'name': row['name'],
        'photoUrls': photo_urls or [],
    }
    if row.get('status') is not None:
        d['status'] = row['status']
    return d


def order_row_to_dict(row):
    if row is None:
        return None
    d = {'id': row['id']}
    if row.get('pet_id') is not None:
        d['petId'] = row['pet_id']
    if row.get('quantity') is not None:
        d['quantity'] = row['quantity']
    if row.get('ship_date') is not None:
        d['shipDate'] = row['ship_date'].isoformat()
    if row.get('status') is not None:
        d['status'] = row['status']
    if row.get('complete') is not None:
        d['complete'] = row['complete']
    return d


def user_row_to_dict(row):
    if row is None:
        return None
    d = {'id': row['id']}
    for src, dst in [
        ('username', 'username'),
        ('first_name', 'firstName'),
        ('last_name', 'lastName'),
        ('email', 'email'),
        ('password', 'password'),
        ('phone', 'phone'),
        ('user_status', 'userStatus'),
    ]:
        if row.get(src) is not None:
            d[dst] = row[src]
    return d


# ----- Pet endpoints -----

@app.route('/pet', methods=['POST'])
def add_pet():
    data = request.get_json(silent=True)
    if not data or 'name' not in data or 'photoUrls' not in data:
        return jsonify({'message': 'Invalid input'}), 400
    name = data['name']
    photo_urls = data['photoUrls']
    status = data.get('status')
    pet_id = data.get('id')
    if not isinstance(name, str) or not isinstance(photo_urls, list):
        return jsonify({'message': 'Invalid input'}), 400
    try:
        with DBConn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if pet_id is not None:
                    cur.execute(
                        """INSERT INTO pets (id, name, photo_urls, status)
                           VALUES (%s, %s, %s::jsonb, %s)
                           ON CONFLICT (id) DO UPDATE SET
                             name = EXCLUDED.name,
                             photo_urls = EXCLUDED.photo_urls,
                             status = EXCLUDED.status
                           RETURNING id, name, photo_urls, status""",
                        (pet_id, name, json.dumps(photo_urls), status),
                    )
                else:
                    cur.execute(
                        """INSERT INTO pets (name, photo_urls, status)
                           VALUES (%s, %s::jsonb, %s)
                           RETURNING id, name, photo_urls, status""",
                        (name, json.dumps(photo_urls), status),
                    )
                row = cur.fetchone()
                conn.commit()
        return jsonify(pet_row_to_dict(row)), 200
    except Exception:
        return jsonify({'message': 'Invalid input'}), 400


@app.route('/pet', methods=['PUT'])
def update_pet():
    data = request.get_json(silent=True)
    if not data or 'name' not in data or 'photoUrls' not in data:
        return jsonify({'message': 'Invalid input'}), 400
    pet_id = data.get('id')
    if pet_id is None:
        return jsonify({'message': 'Pet not found'}), 404
    name = data['name']
    photo_urls = data['photoUrls']
    status = data.get('status')
    try:
        with DBConn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """UPDATE pets SET name = %s, photo_urls = %s::jsonb, status = %s
                       WHERE id = %s
                       RETURNING id, name, photo_urls, status""",
                    (name, json.dumps(photo_urls), status, pet_id),
                )
                row = cur.fetchone()
                conn.commit()
        if row is None:
            return jsonify({'message': 'Pet not found'}), 404
        return jsonify(pet_row_to_dict(row)), 200
    except Exception:
        return jsonify({'message': 'Invalid input'}), 400


@app.route('/pet/findByStatus', methods=['GET'])
def find_pets_by_status():
    status = request.args.get('status')
    if status not in ('available', 'pending', 'sold'):
        return jsonify([]), 200
    with DBConn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, name, photo_urls, status FROM pets WHERE status = %s",
                (status,),
            )
            rows = cur.fetchall()
    return jsonify([pet_row_to_dict(r) for r in rows]), 200


@app.route('/pet/<int:petId>', methods=['GET'])
def get_pet_by_id(petId):
    with DBConn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, name, photo_urls, status FROM pets WHERE id = %s",
                (petId,),
            )
            row = cur.fetchone()
    if row is None:
        return jsonify({'message': 'Pet not found'}), 404
    return jsonify(pet_row_to_dict(row)), 200


@app.route('/pet/<int:petId>', methods=['DELETE'])
def delete_pet(petId):
    with DBConn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pets WHERE id = %s", (petId,))
            deleted = cur.rowcount
            conn.commit()
    if deleted == 0:
        return jsonify({'message': 'Pet not found'}), 404
    return jsonify({'message': 'Pet deleted'}), 200


# ----- Order endpoints -----

@app.route('/store/order', methods=['POST'])
def place_order():
    data = request.get_json(silent=True) or {}
    order_id = data.get('id')
    pet_id = data.get('petId')
    quantity = data.get('quantity')
    ship_date = data.get('shipDate')
    status = data.get('status')
    complete = data.get('complete')
    try:
        with DBConn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if order_id is not None:
                    cur.execute(
                        """INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete)
                           VALUES (%s, %s, %s, %s, %s, %s)
                           ON CONFLICT (id) DO UPDATE SET
                             pet_id = EXCLUDED.pet_id,
                             quantity = EXCLUDED.quantity,
                             ship_date = EXCLUDED.ship_date,
                             status = EXCLUDED.status,
                             complete = EXCLUDED.complete
                           RETURNING id, pet_id, quantity, ship_date, status, complete""",
                        (order_id, pet_id, quantity, ship_date, status, complete),
                    )
                else:
                    cur.execute(
                        """INSERT INTO orders (pet_id, quantity, ship_date, status, complete)
                           VALUES (%s, %s, %s, %s, %s)
                           RETURNING id, pet_id, quantity, ship_date, status, complete""",
                        (pet_id, quantity, ship_date, status, complete),
                    )
                row = cur.fetchone()
                conn.commit()
        return jsonify(order_row_to_dict(row)), 200
    except Exception:
        return jsonify({'message': 'Invalid input'}), 400


@app.route('/store/order/<int:orderId>', methods=['GET'])
def get_order_by_id(orderId):
    with DBConn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id = %s",
                (orderId,),
            )
            row = cur.fetchone()
    if row is None:
        return jsonify({'message': 'Order not found'}), 404
    return jsonify(order_row_to_dict(row)), 200


@app.route('/store/order/<int:orderId>', methods=['DELETE'])
def delete_order(orderId):
    with DBConn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM orders WHERE id = %s", (orderId,))
            deleted = cur.rowcount
            conn.commit()
    if deleted == 0:
        return jsonify({'message': 'Order not found'}), 404
    return jsonify({'message': 'Order deleted'}), 200


# ----- User endpoints -----

@app.route('/user', methods=['POST'])
def create_user():
    data = request.get_json(silent=True) or {}
    user_id = data.get('id')
    username = data.get('username')
    first_name = data.get('firstName')
    last_name = data.get('lastName')
    email = data.get('email')
    password = data.get('password')
    phone = data.get('phone')
    user_status = data.get('userStatus')
    try:
        with DBConn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if user_id is not None:
                    cur.execute(
                        """INSERT INTO users (id, username, first_name, last_name, email, password, phone, user_status)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (id) DO UPDATE SET
                             username = EXCLUDED.username,
                             first_name = EXCLUDED.first_name,
                             last_name = EXCLUDED.last_name,
                             email = EXCLUDED.email,
                             password = EXCLUDED.password,
                             phone = EXCLUDED.phone,
                             user_status = EXCLUDED.user_status
                           RETURNING *""",
                        (user_id, username, first_name, last_name, email, password, phone, user_status),
                    )
                else:
                    cur.execute(
                        """INSERT INTO users (username, first_name, last_name, email, password, phone, user_status)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (username) DO UPDATE SET
                             first_name = EXCLUDED.first_name,
                             last_name = EXCLUDED.last_name,
                             email = EXCLUDED.email,
                             password = EXCLUDED.password,
                             phone = EXCLUDED.phone,
                             user_status = EXCLUDED.user_status
                           RETURNING *""",
                        (username, first_name, last_name, email, password, phone, user_status),
                    )
                row = cur.fetchone()
                conn.commit()
        return jsonify(user_row_to_dict(row)), 200
    except Exception:
        return jsonify({'message': 'Invalid input'}), 400


@app.route('/user/login', methods=['GET'])
def login_user():
    username = request.args.get('username')
    password = request.args.get('password')
    if not username or not password:
        return jsonify({'message': 'Invalid credentials'}), 400
    with DBConn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT password FROM users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
    if row is None or row['password'] != password:
        return jsonify({'message': 'Invalid credentials'}), 400
    return jsonify('logged in'), 200


@app.route('/user/<string:username>', methods=['GET'])
def get_user_by_name(username):
    with DBConn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
    if row is None:
        return jsonify({'message': 'User not found'}), 404
    return jsonify(user_row_to_dict(row)), 200


@app.route('/user/<string:username>', methods=['PUT'])
def update_user(username):
    data = request.get_json(silent=True) or {}
    new_username = data.get('username', username)
    first_name = data.get('firstName')
    last_name = data.get('lastName')
    email = data.get('email')
    password = data.get('password')
    phone = data.get('phone')
    user_status = data.get('userStatus')
    try:
        with DBConn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """UPDATE users SET
                         username = %s,
                         first_name = %s,
                         last_name = %s,
                         email = %s,
                         password = %s,
                         phone = %s,
                         user_status = %s
                       WHERE username = %s
                       RETURNING *""",
                    (new_username, first_name, last_name, email, password, phone, user_status, username),
                )
                row = cur.fetchone()
                conn.commit()
        if row is None:
            return jsonify({'message': 'User not found'}), 404
        return jsonify(user_row_to_dict(row)), 200
    except Exception:
        return jsonify({'message': 'Invalid input'}), 400


@app.route('/user/<string:username>', methods=['DELETE'])
def delete_user(username):
    with DBConn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE username = %s", (username,))
            deleted = cur.rowcount
            conn.commit()
    if deleted == 0:
        return jsonify({'message': 'User not found'}), 404
    return jsonify({'message': 'User deleted'}), 200


@app.errorhandler(404)
def not_found(e):
    return jsonify({'message': 'Not found'}), 404


@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({'message': 'Method not allowed'}), 405


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)
