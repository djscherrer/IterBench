const express = require('express');
const { Pool } = require('pg');
const dotenv = require('dotenv');

dotenv.config();

const app = express();
app.use(express.json());

const pool = new Pool({
  host: process.env.DB_HOST || 'localhost',
  port: parseInt(process.env.DB_PORT || '5432'),
  user: process.env.DB_USER || 'postgres',
  password: process.env.DB_PASSWORD || 'postgres',
  database: process.env.DB_NAME || 'petstore',
});

async function initializeDatabase() {
  const client = await pool.connect();
  try {
    // Use advisory lock to prevent concurrent initialization issues
    await client.query('SELECT pg_advisory_lock(12345)');
    
    await client.query(`
      CREATE TABLE IF NOT EXISTS pets (
        id BIGSERIAL PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        photo_urls TEXT[] NOT NULL DEFAULT '{}',
        status VARCHAR(20) CHECK (status IN ('available', 'pending', 'sold'))
      );
    `);

    await client.query(`
      CREATE TABLE IF NOT EXISTS orders (
        id BIGSERIAL PRIMARY KEY,
        pet_id BIGINT,
        quantity INTEGER,
        ship_date TIMESTAMPTZ,
        status VARCHAR(20) CHECK (status IN ('placed', 'approved', 'delivered')),
        complete BOOLEAN DEFAULT false
      );
    `);

    await client.query(`
      CREATE TABLE IF NOT EXISTS users (
        id BIGSERIAL PRIMARY KEY,
        username VARCHAR(255) UNIQUE,
        first_name VARCHAR(255),
        last_name VARCHAR(255),
        email VARCHAR(255),
        password VARCHAR(255),
        phone VARCHAR(255),
        user_status INTEGER
      );
    `);

    await client.query('SELECT pg_advisory_unlock(12345)');
  } catch (err) {
    await client.query('SELECT pg_advisory_unlock(12345)').catch(() => {});
    throw err;
  } finally {
    client.release();
  }
}

// Helper functions
function petRowToJson(row) {
  return {
    id: parseInt(row.id),
    name: row.name,
    photoUrls: row.photo_urls || [],
    status: row.status
  };
}

function orderRowToJson(row) {
  return {
    id: parseInt(row.id),
    petId: row.pet_id ? parseInt(row.pet_id) : undefined,
    quantity: row.quantity,
    shipDate: row.ship_date ? row.ship_date.toISOString() : undefined,
    status: row.status,
    complete: row.complete
  };
}

function userRowToJson(row) {
  return {
    id: parseInt(row.id),
    username: row.username,
    firstName: row.first_name,
    lastName: row.last_name,
    email: row.email,
    password: row.password,
    phone: row.phone,
    userStatus: row.user_status
  };
}

// ==================== PET ENDPOINTS ====================

// Add a new pet
app.post('/pet', async (req, res) => {
  try {
    const { name, photoUrls, status } = req.body;
    if (!name || !photoUrls) {
      return res.status(400).json({ message: 'Invalid input' });
    }
    const result = await pool.query(
      'INSERT INTO pets (name, photo_urls, status) VALUES ($1, $2, $3) RETURNING *',
      [name, photoUrls, status || null]
    );
    res.status(200).json(petRowToJson(result.rows[0]));
  } catch (err) {
    console.error(err);
    res.status(400).json({ message: 'Invalid input' });
  }
});

// Update an existing pet
app.put('/pet', async (req, res) => {
  try {
    const { id, name, photoUrls, status } = req.body;
    if (!id) {
      return res.status(404).json({ message: 'Pet not found' });
    }
    const result = await pool.query(
      'UPDATE pets SET name = $1, photo_urls = $2, status = $3 WHERE id = $4 RETURNING *',
      [name, photoUrls || [], status || null, id]
    );
    if (result.rows.length === 0) {
      return res.status(404).json({ message: 'Pet not found' });
    }
    res.status(200).json(petRowToJson(result.rows[0]));
  } catch (err) {
    console.error(err);
    res.status(404).json({ message: 'Pet not found' });
  }
});

// Find pets by status
app.get('/pet/findByStatus', async (req, res) => {
  try {
    const { status } = req.query;
    if (!status || !['available', 'pending', 'sold'].includes(status)) {
      return res.status(200).json([]);
    }
    const result = await pool.query(
      'SELECT * FROM pets WHERE status = $1',
      [status]
    );
    res.status(200).json(result.rows.map(petRowToJson));
  } catch (err) {
    console.error(err);
    res.status(200).json([]);
  }
});

// Get pet by ID
app.get('/pet/:petId', async (req, res) => {
  try {
    const petId = parseInt(req.params.petId);
    if (isNaN(petId)) {
      return res.status(404).json({ message: 'Pet not found' });
    }
    const result = await pool.query('SELECT * FROM pets WHERE id = $1', [petId]);
    if (result.rows.length === 0) {
      return res.status(404).json({ message: 'Pet not found' });
    }
    res.status(200).json(petRowToJson(result.rows[0]));
  } catch (err) {
    console.error(err);
    res.status(404).json({ message: 'Pet not found' });
  }
});

// Delete a pet
app.delete('/pet/:petId', async (req, res) => {
  try {
    const petId = parseInt(req.params.petId);
    if (isNaN(petId)) {
      return res.status(404).json({ message: 'Pet not found' });
    }
    const result = await pool.query('DELETE FROM pets WHERE id = $1 RETURNING *', [petId]);
    if (result.rows.length === 0) {
      return res.status(404).json({ message: 'Pet not found' });
    }
    res.status(200).json({ message: 'successful operation' });
  } catch (err) {
    console.error(err);
    res.status(404).json({ message: 'Pet not found' });
  }
});

// ==================== STORE/ORDER ENDPOINTS ====================

// Place an order
app.post('/store/order', async (req, res) => {
  try {
    const { petId, quantity, shipDate, status, complete } = req.body;
    const result = await pool.query(
      'INSERT INTO orders (pet_id, quantity, ship_date, status, complete) VALUES ($1, $2, $3, $4, $5) RETURNING *',
      [petId || null, quantity || null, shipDate || null, status || null, complete || false]
    );
    res.status(200).json(orderRowToJson(result.rows[0]));
  } catch (err) {
    console.error(err);
    res.status(400).json({ message: 'Invalid order' });
  }
});

// Get order by ID
app.get('/store/order/:orderId', async (req, res) => {
  try {
    const orderId = parseInt(req.params.orderId);
    if (isNaN(orderId)) {
      return res.status(404).json({ message: 'Order not found' });
    }
    const result = await pool.query('SELECT * FROM orders WHERE id = $1', [orderId]);
    if (result.rows.length === 0) {
      return res.status(404).json({ message: 'Order not found' });
    }
    res.status(200).json(orderRowToJson(result.rows[0]));
  } catch (err) {
    console.error(err);
    res.status(404).json({ message: 'Order not found' });
  }
});

// Delete order by ID
app.delete('/store/order/:orderId', async (req, res) => {
  try {
    const orderId = parseInt(req.params.orderId);
    if (isNaN(orderId)) {
      return res.status(404).json({ message: 'Order not found' });
    }
    const result = await pool.query('DELETE FROM orders WHERE id = $1 RETURNING *', [orderId]);
    if (result.rows.length === 0) {
      return res.status(404).json({ message: 'Order not found' });
    }
    res.status(200).json({ message: 'successful operation' });
  } catch (err) {
    console.error(err);
    res.status(404).json({ message: 'Order not found' });
  }
});

// ==================== USER ENDPOINTS ====================

// Login user - must be before /user/:username to avoid route conflict
app.get('/user/login', async (req, res) => {
  try {
    const { username, password } = req.query;
    if (!username || !password) {
      return res.status(400).json({ message: 'Invalid credentials' });
    }
    const result = await pool.query(
      'SELECT * FROM users WHERE username = $1 AND password = $2',
      [username, password]
    );
    if (result.rows.length === 0) {
      return res.status(400).json({ message: 'Invalid credentials' });
    }
    res.status(200).json('Logged in successfully');
  } catch (err) {
    console.error(err);
    res.status(400).json({ message: 'Invalid credentials' });
  }
});

// Create user
app.post('/user', async (req, res) => {
  try {
    const { username, firstName, lastName, email, password, phone, userStatus } = req.body;
    const result = await pool.query(
      'INSERT INTO users (username, first_name, last_name, email, password, phone, user_status) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *',
      [username || null, firstName || null, lastName || null, email || null, password || null, phone || null, userStatus || null]
    );
    res.status(200).json(userRowToJson(result.rows[0]));
  } catch (err) {
    console.error(err);
    res.status(400).json({ message: 'Invalid user' });
  }
});

// Get user by username
app.get('/user/:username', async (req, res) => {
  try {
    const { username } = req.params;
    const result = await pool.query('SELECT * FROM users WHERE username = $1', [username]);
    if (result.rows.length === 0) {
      return res.status(404).json({ message: 'User not found' });
    }
    res.status(200).json(userRowToJson(result.rows[0]));
  } catch (err) {
    console.error(err);
    res.status(404).json({ message: 'User not found' });
  }
});

// Update user
app.put('/user/:username', async (req, res) => {
  try {
    const { username } = req.params;
    const { firstName, lastName, email, password, phone, userStatus, username: newUsername } = req.body;
    
    // Check if user exists
    const existing = await pool.query('SELECT * FROM users WHERE username = $1', [username]);
    if (existing.rows.length === 0) {
      return res.status(404).json({ message: 'User not found' });
    }

    const result = await pool.query(
      'UPDATE users SET username = $1, first_name = $2, last_name = $3, email = $4, password = $5, phone = $6, user_status = $7 WHERE username = $8 RETURNING *',
      [newUsername || username, firstName || null, lastName || null, email || null, password || null, phone || null, userStatus || null, username]
    );
    res.status(200).json(userRowToJson(result.rows[0]));
  } catch (err) {
    console.error(err);
    res.status(404).json({ message: 'User not found' });
  }
});

// Delete user
app.delete('/user/:username', async (req, res) => {
  try {
    const { username } = req.params;
    const result = await pool.query('DELETE FROM users WHERE username = $1 RETURNING *', [username]);
    if (result.rows.length === 0) {
      return res.status(404).json({ message: 'User not found' });
    }
    res.status(200).json({ message: 'successful operation' });
  } catch (err) {
    console.error(err);
    res.status(404).json({ message: 'User not found' });
  }
});

// ==================== START SERVER ====================

const PORT = parseInt(process.env.PORT || '5001');

initializeDatabase()
  .then(() => {
    app.listen(PORT, '0.0.0.0', () => {
      console.log(`Server running on port ${PORT}`);
    });
  })
  .catch((err) => {
    console.error('Failed to initialize database:', err);
    process.exit(1);
  });