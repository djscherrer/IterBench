require('dotenv').config();
const express = require('express');
const { Pool } = require('pg');

const app = express();
app.use(express.json());

const PORT = parseInt(process.env.PORT, 10) || 5001;

const pool = new Pool({
  host: process.env.DB_HOST,
  port: process.env.DB_PORT ? parseInt(process.env.DB_PORT, 10) : 5432,
  user: process.env.DB_USER,
  password: process.env.DB_PASSWORD,
  database: process.env.DB_NAME,
});

function toInt64(value) {
  if (value === undefined || value === null || value === '') return null;
  const num = Number(value);
  if (!Number.isInteger(num)) return null;
  return num;
}

function isValidPetStatus(status) {
  return ['available', 'pending', 'sold'].includes(status);
}

function isValidOrderStatus(status) {
  return ['placed', 'approved', 'delivered'].includes(status);
}

function validatePet(body, requireAll = true) {
  if (typeof body !== 'object' || body === null) {
    return 'Invalid input';
  }

  if (requireAll) {
    if (typeof body.name !== 'string' || body.name.trim() === '') {
      return 'Invalid input';
    }
    if (!Array.isArray(body.photoUrls)) {
      return 'Invalid input';
    }
  }

  if (body.name !== undefined && (typeof body.name !== 'string' || body.name.trim() === '')) {
    return 'Invalid input';
  }

  if (body.photoUrls !== undefined) {
    if (!Array.isArray(body.photoUrls) || !body.photoUrls.every((v) => typeof v === 'string')) {
      return 'Invalid input';
    }
  }

  if (body.id !== undefined && toInt64(body.id) === null) {
    return 'Invalid input';
  }

  if (body.status !== undefined && body.status !== null && !isValidPetStatus(body.status)) {
    return 'Invalid input';
  }

  return null;
}

function validateOrder(body) {
  if (typeof body !== 'object' || body === null) {
    return 'Invalid input';
  }

  if (body.id !== undefined && toInt64(body.id) === null) return 'Invalid input';
  if (body.petId !== undefined && toInt64(body.petId) === null) return 'Invalid input';
  if (body.quantity !== undefined && !Number.isInteger(Number(body.quantity))) return 'Invalid input';
  if (body.shipDate !== undefined && body.shipDate !== null && Number.isNaN(Date.parse(body.shipDate))) return 'Invalid input';
  if (body.status !== undefined && body.status !== null && !isValidOrderStatus(body.status)) return 'Invalid input';
  if (body.complete !== undefined && typeof body.complete !== 'boolean') return 'Invalid input';

  return null;
}

function validateUser(body) {
  if (typeof body !== 'object' || body === null) {
    return 'Invalid input';
  }

  if (body.id !== undefined && toInt64(body.id) === null) return 'Invalid input';
  if (body.username !== undefined && typeof body.username !== 'string') return 'Invalid input';
  if (body.firstName !== undefined && typeof body.firstName !== 'string') return 'Invalid input';
  if (body.lastName !== undefined && typeof body.lastName !== 'string') return 'Invalid input';
  if (body.email !== undefined && typeof body.email !== 'string') return 'Invalid input';
  if (body.password !== undefined && typeof body.password !== 'string') return 'Invalid input';
  if (body.phone !== undefined && typeof body.phone !== 'string') return 'Invalid input';
  if (body.userStatus !== undefined && !Number.isInteger(Number(body.userStatus))) return 'Invalid input';

  return null;
}

function mapPet(row) {
  if (!row) return null;
  return {
    id: Number(row.id),
    name: row.name,
    photoUrls: row.photo_urls || [],
    status: row.status,
  };
}

function mapOrder(row) {
  if (!row) return null;
  return {
    id: Number(row.id),
    petId: row.pet_id === null ? null : Number(row.pet_id),
    quantity: row.quantity,
    shipDate: row.ship_date ? new Date(row.ship_date).toISOString() : null,
    status: row.status,
    complete: row.complete,
  };
}

function mapUser(row) {
  if (!row) return null;
  return {
    id: Number(row.id),
    username: row.username,
    firstName: row.first_name,
    lastName: row.last_name,
    email: row.email,
    password: row.password,
    phone: row.phone,
    userStatus: row.user_status,
  };
}

async function initDb() {
  await pool.query(`
    CREATE TABLE IF NOT EXISTS pets (
      id BIGINT PRIMARY KEY,
      name TEXT NOT NULL,
      photo_urls TEXT[] NOT NULL DEFAULT '{}',
      status TEXT CHECK (status IN ('available', 'pending', 'sold') OR status IS NULL)
    )
  `);

  await pool.query(`
    CREATE SEQUENCE IF NOT EXISTS pets_id_seq
  `);

  await pool.query(`
    CREATE TABLE IF NOT EXISTS orders (
      id BIGINT PRIMARY KEY,
      pet_id BIGINT,
      quantity INTEGER,
      ship_date TIMESTAMPTZ,
      status TEXT CHECK (status IN ('placed', 'approved', 'delivered') OR status IS NULL),
      complete BOOLEAN
    )
  `);

  await pool.query(`
    CREATE SEQUENCE IF NOT EXISTS orders_id_seq
  `);

  await pool.query(`
    CREATE TABLE IF NOT EXISTS users (
      id BIGINT PRIMARY KEY,
      username TEXT UNIQUE NOT NULL,
      first_name TEXT,
      last_name TEXT,
      email TEXT,
      password TEXT,
      phone TEXT,
      user_status INTEGER
    )
  `);

  await pool.query(`
    CREATE SEQUENCE IF NOT EXISTS users_id_seq
  `);

  await pool.query(`
    SELECT setval(
      'pets_id_seq',
      GREATEST((SELECT COALESCE(MAX(id), 0) FROM pets), 1),
      true
    )
  `);

  await pool.query(`
    SELECT setval(
      'orders_id_seq',
      GREATEST((SELECT COALESCE(MAX(id), 0) FROM orders), 1),
      true
    )
  `);

  await pool.query(`
    SELECT setval(
      'users_id_seq',
      GREATEST((SELECT COALESCE(MAX(id), 0) FROM users), 1),
      true
    )
  `);
}

// Pet routes
app.post('/pet', async (req, res) => {
  const error = validatePet(req.body, true);
  if (error) {
    return res.status(400).json({ message: error });
  }

  const id = req.body.id !== undefined ? toInt64(req.body.id) : null;

  try {
    const result = await pool.query(
      `
      INSERT INTO pets (id, name, photo_urls, status)
      VALUES (COALESCE($1, nextval('pets_id_seq')), $2, $3, $4)
      RETURNING id, name, photo_urls, status
      `,
      [
        id,
        req.body.name,
        req.body.photoUrls || [],
        req.body.status || null,
      ]
    );

    return res.status(200).json(mapPet(result.rows[0]));
  } catch (err) {
    if (err.code === '23505') {
      return res.status(400).json({ message: 'Invalid input' });
    }
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.put('/pet', async (req, res) => {
  const error = validatePet(req.body, true);
  if (error) {
    return res.status(400).json({ message: error });
  }

  const id = toInt64(req.body.id);
  if (id === null) {
    return res.status(400).json({ message: 'Invalid input' });
  }

  try {
    const result = await pool.query(
      `
      UPDATE pets
      SET name = $2, photo_urls = $3, status = $4
      WHERE id = $1
      RETURNING id, name, photo_urls, status
      `,
      [
        id,
        req.body.name,
        req.body.photoUrls || [],
        req.body.status || null,
      ]
    );

    if (result.rows.length === 0) {
      return res.status(404).json({ message: 'Pet not found' });
    }

    return res.status(200).json(mapPet(result.rows[0]));
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.get('/pet/findByStatus', async (req, res) => {
  const { status } = req.query;
  if (!isValidPetStatus(status)) {
    return res.status(400).json({ message: 'Invalid status value' });
  }

  try {
    const result = await pool.query(
      `SELECT id, name, photo_urls, status FROM pets WHERE status = $1 ORDER BY id`,
      [status]
    );
    return res.status(200).json(result.rows.map(mapPet));
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.get('/pet/:petId', async (req, res) => {
  const petId = toInt64(req.params.petId);
  if (petId === null) {
    return res.status(404).json({ message: 'Pet not found' });
  }

  try {
    const result = await pool.query(
      `SELECT id, name, photo_urls, status FROM pets WHERE id = $1`,
      [petId]
    );

    if (result.rows.length === 0) {
      return res.status(404).json({ message: 'Pet not found' });
    }

    return res.status(200).json(mapPet(result.rows[0]));
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.delete('/pet/:petId', async (req, res) => {
  const petId = toInt64(req.params.petId);
  if (petId === null) {
    return res.status(404).json({ message: 'Pet not found' });
  }

  try {
    const result = await pool.query(`DELETE FROM pets WHERE id = $1 RETURNING id`, [petId]);
    if (result.rows.length === 0) {
      return res.status(404).json({ message: 'Pet not found' });
    }
    return res.status(200).end();
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

// Store order routes
app.post('/store/order', async (req, res) => {
  const error = validateOrder(req.body);
  if (error) {
    return res.status(400).json({ message: error });
  }

  const id = req.body.id !== undefined ? toInt64(req.body.id) : null;
  const petId = req.body.petId !== undefined ? toInt64(req.body.petId) : null;
  const quantity = req.body.quantity !== undefined ? Number(req.body.quantity) : null;
  const shipDate = req.body.shipDate !== undefined && req.body.shipDate !== null ? new Date(req.body.shipDate).toISOString() : null;

  try {
    const result = await pool.query(
      `
      INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete)
      VALUES (COALESCE($1, nextval('orders_id_seq')), $2, $3, $4, $5, $6)
      RETURNING id, pet_id, quantity, ship_date, status, complete
      `,
      [
        id,
        petId,
        quantity,
        shipDate,
        req.body.status || null,
        req.body.complete !== undefined ? req.body.complete : null,
      ]
    );

    return res.status(200).json(mapOrder(result.rows[0]));
  } catch (err) {
    if (err.code === '23505') {
      return res.status(400).json({ message: 'Invalid input' });
    }
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.get('/store/order/:orderId', async (req, res) => {
  const orderId = toInt64(req.params.orderId);
  if (orderId === null) {
    return res.status(404).json({ message: 'Order not found' });
  }

  try {
    const result = await pool.query(
      `SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id = $1`,
      [orderId]
    );

    if (result.rows.length === 0) {
      return res.status(404).json({ message: 'Order not found' });
    }

    return res.status(200).json(mapOrder(result.rows[0]));
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.delete('/store/order/:orderId', async (req, res) => {
  const orderId = toInt64(req.params.orderId);
  if (orderId === null) {
    return res.status(404).json({ message: 'Order not found' });
  }

  try {
    const result = await pool.query(`DELETE FROM orders WHERE id = $1 RETURNING id`, [orderId]);
    if (result.rows.length === 0) {
      return res.status(404).json({ message: 'Order not found' });
    }
    return res.status(200).end();
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

// User routes
app.post('/user', async (req, res) => {
  const error = validateUser(req.body);
  if (error) {
    return res.status(400).json({ message: error });
  }

  if (req.body.username === undefined || typeof req.body.username !== 'string' || req.body.username.trim() === '') {
    return res.status(400).json({ message: 'Invalid input' });
  }

  const id = req.body.id !== undefined ? toInt64(req.body.id) : null;
  const userStatus = req.body.userStatus !== undefined ? Number(req.body.userStatus) : null;

  try {
    const result = await pool.query(
      `
      INSERT INTO users (id, username, first_name, last_name, email, password, phone, user_status)
      VALUES (COALESCE($1, nextval('users_id_seq')), $2, $3, $4, $5, $6, $7, $8)
      RETURNING id, username, first_name, last_name, email, password, phone, user_status
      `,
      [
        id,
        req.body.username,
        req.body.firstName || null,
        req.body.lastName || null,
        req.body.email || null,
        req.body.password || null,
        req.body.phone || null,
        userStatus,
      ]
    );

    return res.status(200).json(mapUser(result.rows[0]));
  } catch (err) {
    if (err.code === '23505') {
      return res.status(400).json({ message: 'Invalid input' });
    }
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.get('/user/login', async (req, res) => {
  const { username, password } = req.query;

  if (typeof username !== 'string' || typeof password !== 'string') {
    return res.status(400).json({ message: 'Invalid credentials' });
  }

  try {
    const result = await pool.query(
      `SELECT id FROM users WHERE username = $1 AND password = $2`,
      [username, password]
    );

    if (result.rows.length === 0) {
      return res.status(400).json({ message: 'Invalid credentials' });
    }

    return res.status(200).json(`User logged in successfully`);
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.get('/user/:username', async (req, res) => {
  const { username } = req.params;

  try {
    const result = await pool.query(
      `SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username = $1`,
      [username]
    );

    if (result.rows.length === 0) {
      return res.status(404).json({ message: 'User not found' });
    }

    return res.status(200).json(mapUser(result.rows[0]));
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.put('/user/:username', async (req, res) => {
  const error = validateUser(req.body);
  if (error) {
    return res.status(400).json({ message: error });
  }

  const { username } = req.params;
  const id = req.body.id !== undefined ? toInt64(req.body.id) : null;
  const userStatus = req.body.userStatus !== undefined ? Number(req.body.userStatus) : null;
  const newUsername = req.body.username !== undefined ? req.body.username : username;

  if (typeof newUsername !== 'string' || newUsername.trim() === '') {
    return res.status(400).json({ message: 'Invalid input' });
  }

  try {
    const result = await pool.query(
      `
      UPDATE users
      SET
        id = COALESCE($2, id),
        username = $3,
        first_name = $4,
        last_name = $5,
        email = $6,
        password = $7,
        phone = $8,
        user_status = $9
      WHERE username = $1
      RETURNING id, username, first_name, last_name, email, password, phone, user_status
      `,
      [
        username,
        id,
        newUsername,
        req.body.firstName !== undefined ? req.body.firstName : null,
        req.body.lastName !== undefined ? req.body.lastName : null,
        req.body.email !== undefined ? req.body.email : null,
        req.body.password !== undefined ? req.body.password : null,
        req.body.phone !== undefined ? req.body.phone : null,
        userStatus,
      ]
    );

    if (result.rows.length === 0) {
      return res.status(404).json({ message: 'User not found' });
    }

    return res.status(200).json(mapUser(result.rows[0]));
  } catch (err) {
    if (err.code === '23505') {
      return res.status(400).json({ message: 'Invalid input' });
    }
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.delete('/user/:username', async (req, res) => {
  const { username } = req.params;

  try {
    const result = await pool.query(`DELETE FROM users WHERE username = $1 RETURNING username`, [username]);
    if (result.rows.length === 0) {
      return res.status(404).json({ message: 'User not found' });
    }
    return res.status(200).end();
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.use((req, res) => {
  res.status(404).json({ message: 'Not found' });
});

(async () => {
  try {
    await initDb();
    app.listen(PORT, '0.0.0.0', () => {
      console.log(`Petstore API listening on 0.0.0.0:${PORT}`);
    });
  } catch (err) {
    console.error('Failed to initialize application', err);
    process.exit(1);
  }
})();