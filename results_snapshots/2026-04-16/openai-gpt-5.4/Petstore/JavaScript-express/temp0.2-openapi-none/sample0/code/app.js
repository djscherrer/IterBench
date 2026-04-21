require('dotenv').config();
const express = require('express');
const { Pool } = require('pg');

const app = express();
app.use(express.json());

const PORT = parseInt(process.env.PORT || '5001', 10);

const pool = new Pool({
  host: process.env.DB_HOST,
  port: process.env.DB_PORT ? parseInt(process.env.DB_PORT, 10) : 5432,
  user: process.env.DB_USER,
  password: process.env.DB_PASSWORD,
  database: process.env.DB_NAME,
});

function isIntegerLike(value) {
  return /^-?\d+$/.test(String(value));
}

function parseIdParam(value) {
  if (!isIntegerLike(value)) return null;
  return parseInt(value, 10);
}

function validatePet(body) {
  if (!body || typeof body !== 'object') {
    return 'Invalid input';
  }

  if (typeof body.name !== 'string' || body.name.trim() === '') {
    return 'Pet name is required';
  }

  if (!Array.isArray(body.photoUrls)) {
    return 'photoUrls is required and must be an array';
  }

  for (const url of body.photoUrls) {
    if (typeof url !== 'string') {
      return 'photoUrls must contain only strings';
    }
  }

  if (
    body.status !== undefined &&
    !['available', 'pending', 'sold'].includes(body.status)
  ) {
    return 'Invalid pet status';
  }

  if (body.id !== undefined && !Number.isInteger(body.id)) {
    return 'Pet id must be an integer';
  }

  return null;
}

function validateOrder(body) {
  if (!body || typeof body !== 'object') {
    return 'Invalid input';
  }

  if (body.id !== undefined && !Number.isInteger(body.id)) {
    return 'Order id must be an integer';
  }

  if (body.petId !== undefined && !Number.isInteger(body.petId)) {
    return 'petId must be an integer';
  }

  if (body.quantity !== undefined && !Number.isInteger(body.quantity)) {
    return 'quantity must be an integer';
  }

  if (
    body.status !== undefined &&
    !['placed', 'approved', 'delivered'].includes(body.status)
  ) {
    return 'Invalid order status';
  }

  if (body.complete !== undefined && typeof body.complete !== 'boolean') {
    return 'complete must be a boolean';
  }

  if (body.shipDate !== undefined) {
    const date = new Date(body.shipDate);
    if (Number.isNaN(date.getTime())) {
      return 'shipDate must be a valid date-time string';
    }
  }

  return null;
}

function validateUser(body) {
  if (!body || typeof body !== 'object') {
    return 'Invalid input';
  }

  const stringFields = [
    'username',
    'firstName',
    'lastName',
    'email',
    'password',
    'phone',
  ];

  for (const field of stringFields) {
    if (body[field] !== undefined && typeof body[field] !== 'string') {
      return `${field} must be a string`;
    }
  }

  if (body.id !== undefined && !Number.isInteger(body.id)) {
    return 'User id must be an integer';
  }

  if (body.userStatus !== undefined && !Number.isInteger(body.userStatus)) {
    return 'userStatus must be an integer';
  }

  return null;
}

function mapPet(row) {
  return {
    id: row.id,
    name: row.name,
    photoUrls: row.photo_urls || [],
    status: row.status,
  };
}

function mapOrder(row) {
  return {
    id: row.id,
    petId: row.pet_id,
    quantity: row.quantity,
    shipDate: row.ship_date ? new Date(row.ship_date).toISOString() : null,
    status: row.status,
    complete: row.complete,
  };
}

function mapUser(row) {
  return {
    id: row.id,
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
      photo_urls TEXT[] NOT NULL,
      status TEXT CHECK (status IN ('available', 'pending', 'sold') OR status IS NULL)
    )
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
    CREATE SEQUENCE IF NOT EXISTS pets_id_seq START 1
  `);

  await pool.query(`
    CREATE SEQUENCE IF NOT EXISTS orders_id_seq START 1
  `);

  await pool.query(`
    CREATE SEQUENCE IF NOT EXISTS users_id_seq START 1
  `);

  await pool.query(`
    SELECT setval(
      'pets_id_seq',
      GREATEST((SELECT COALESCE(MAX(id), 0) FROM pets), 0) + 1,
      false
    )
  `);

  await pool.query(`
    SELECT setval(
      'orders_id_seq',
      GREATEST((SELECT COALESCE(MAX(id), 0) FROM orders), 0) + 1,
      false
    )
  `);

  await pool.query(`
    SELECT setval(
      'users_id_seq',
      GREATEST((SELECT COALESCE(MAX(id), 0) FROM users), 0) + 1,
      false
    )
  `);
}

// Pet routes
app.post('/pet', async (req, res) => {
  const error = validatePet(req.body);
  if (error) {
    return res.status(400).json({ message: error });
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    let id = req.body.id;
    if (id === undefined || id === null) {
      const seq = await client.query(`SELECT nextval('pets_id_seq') AS id`);
      id = Number(seq.rows[0].id);
    }

    const result = await client.query(
      `
      INSERT INTO pets (id, name, photo_urls, status)
      VALUES ($1, $2, $3, $4)
      RETURNING id, name, photo_urls, status
      `,
      [id, req.body.name, req.body.photoUrls, req.body.status || null]
    );

    await client.query(
      `
      SELECT setval(
        'pets_id_seq',
        GREATEST((SELECT COALESCE(MAX(id), 0) FROM pets), 0) + 1,
        false
      )
      `
    );

    await client.query('COMMIT');
    return res.status(200).json(mapPet(result.rows[0]));
  } catch (err) {
    await client.query('ROLLBACK');
    if (err.code === '23505') {
      return res.status(400).json({ message: 'Pet with this id already exists' });
    }
    return res.status(500).json({ message: 'Internal server error' });
  } finally {
    client.release();
  }
});

app.put('/pet', async (req, res) => {
  const error = validatePet(req.body);
  if (error) {
    return res.status(400).json({ message: error });
  }

  if (req.body.id === undefined || req.body.id === null) {
    return res.status(400).json({ message: 'Pet id is required for update' });
  }

  try {
    const result = await pool.query(
      `
      UPDATE pets
      SET name = $2, photo_urls = $3, status = $4
      WHERE id = $1
      RETURNING id, name, photo_urls, status
      `,
      [req.body.id, req.body.name, req.body.photoUrls, req.body.status || null]
    );

    if (result.rowCount === 0) {
      return res.status(404).json({ message: 'Pet not found' });
    }

    return res.status(200).json(mapPet(result.rows[0]));
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.get('/pet/findByStatus', async (req, res) => {
  const { status } = req.query;

  if (!status || !['available', 'pending', 'sold'].includes(status)) {
    return res.status(400).json({ message: 'Invalid status' });
  }

  try {
    const result = await pool.query(
      `
      SELECT id, name, photo_urls, status
      FROM pets
      WHERE status = $1
      ORDER BY id
      `,
      [status]
    );

    return res.status(200).json(result.rows.map(mapPet));
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.get('/pet/:petId', async (req, res) => {
  const petId = parseIdParam(req.params.petId);
  if (petId === null) {
    return res.status(404).json({ message: 'Pet not found' });
  }

  try {
    const result = await pool.query(
      `
      SELECT id, name, photo_urls, status
      FROM pets
      WHERE id = $1
      `,
      [petId]
    );

    if (result.rowCount === 0) {
      return res.status(404).json({ message: 'Pet not found' });
    }

    return res.status(200).json(mapPet(result.rows[0]));
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.delete('/pet/:petId', async (req, res) => {
  const petId = parseIdParam(req.params.petId);
  if (petId === null) {
    return res.status(404).json({ message: 'Pet not found' });
  }

  try {
    const result = await pool.query(
      `
      DELETE FROM pets
      WHERE id = $1
      `,
      [petId]
    );

    if (result.rowCount === 0) {
      return res.status(404).json({ message: 'Pet not found' });
    }

    return res.status(200).json({ message: 'successful operation' });
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

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    let id = req.body.id;
    if (id === undefined || id === null) {
      const seq = await client.query(`SELECT nextval('orders_id_seq') AS id`);
      id = Number(seq.rows[0].id);
    }

    const result = await client.query(
      `
      INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete)
      VALUES ($1, $2, $3, $4, $5, $6)
      RETURNING id, pet_id, quantity, ship_date, status, complete
      `,
      [
        id,
        req.body.petId ?? null,
        req.body.quantity ?? null,
        req.body.shipDate ? new Date(req.body.shipDate) : null,
        req.body.status ?? null,
        req.body.complete ?? null,
      ]
    );

    await client.query(
      `
      SELECT setval(
        'orders_id_seq',
        GREATEST((SELECT COALESCE(MAX(id), 0) FROM orders), 0) + 1,
        false
      )
      `
    );

    await client.query('COMMIT');
    return res.status(200).json(mapOrder(result.rows[0]));
  } catch (err) {
    await client.query('ROLLBACK');
    if (err.code === '23505') {
      return res.status(400).json({ message: 'Order with this id already exists' });
    }
    return res.status(500).json({ message: 'Internal server error' });
  } finally {
    client.release();
  }
});

app.get('/store/order/:orderId', async (req, res) => {
  const orderId = parseIdParam(req.params.orderId);
  if (orderId === null) {
    return res.status(404).json({ message: 'Order not found' });
  }

  try {
    const result = await pool.query(
      `
      SELECT id, pet_id, quantity, ship_date, status, complete
      FROM orders
      WHERE id = $1
      `,
      [orderId]
    );

    if (result.rowCount === 0) {
      return res.status(404).json({ message: 'Order not found' });
    }

    return res.status(200).json(mapOrder(result.rows[0]));
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.delete('/store/order/:orderId', async (req, res) => {
  const orderId = parseIdParam(req.params.orderId);
  if (orderId === null) {
    return res.status(404).json({ message: 'Order not found' });
  }

  try {
    const result = await pool.query(
      `
      DELETE FROM orders
      WHERE id = $1
      `,
      [orderId]
    );

    if (result.rowCount === 0) {
      return res.status(404).json({ message: 'Order not found' });
    }

    return res.status(200).json({ message: 'successful operation' });
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

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    let id = req.body.id;
    if (id === undefined || id === null) {
      const seq = await client.query(`SELECT nextval('users_id_seq') AS id`);
      id = Number(seq.rows[0].id);
    }

    const username = req.body.username || `user_${id}`;

    const result = await client.query(
      `
      INSERT INTO users (
        id, username, first_name, last_name, email, password, phone, user_status
      )
      VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
      RETURNING id, username, first_name, last_name, email, password, phone, user_status
      `,
      [
        id,
        username,
        req.body.firstName ?? null,
        req.body.lastName ?? null,
        req.body.email ?? null,
        req.body.password ?? null,
        req.body.phone ?? null,
        req.body.userStatus ?? null,
      ]
    );

    await client.query(
      `
      SELECT setval(
        'users_id_seq',
        GREATEST((SELECT COALESCE(MAX(id), 0) FROM users), 0) + 1,
        false
      )
      `
    );

    await client.query('COMMIT');
    return res.status(200).json(mapUser(result.rows[0]));
  } catch (err) {
    await client.query('ROLLBACK');
    if (err.code === '23505') {
      return res.status(400).json({ message: 'User with this id or username already exists' });
    }
    return res.status(500).json({ message: 'Internal server error' });
  } finally {
    client.release();
  }
});

app.get('/user/login', async (req, res) => {
  const { username, password } = req.query;

  if (typeof username !== 'string' || typeof password !== 'string') {
    return res.status(400).json({ message: 'Invalid credentials' });
  }

  try {
    const result = await pool.query(
      `
      SELECT username
      FROM users
      WHERE username = $1 AND password = $2
      `,
      [username, password]
    );

    if (result.rowCount === 0) {
      return res.status(400).json({ message: 'Invalid credentials' });
    }

    return res.status(200).json(`User logged in successfully: ${username}`);
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.get('/user/:username', async (req, res) => {
  try {
    const result = await pool.query(
      `
      SELECT id, username, first_name, last_name, email, password, phone, user_status
      FROM users
      WHERE username = $1
      `,
      [req.params.username]
    );

    if (result.rowCount === 0) {
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

  try {
    const existing = await pool.query(
      `
      SELECT id
      FROM users
      WHERE username = $1
      `,
      [req.params.username]
    );

    if (existing.rowCount === 0) {
      return res.status(404).json({ message: 'User not found' });
    }

    const currentId = existing.rows[0].id;
    const newUsername = req.body.username ?? req.params.username;
    const newId = req.body.id ?? currentId;

    const result = await pool.query(
      `
      UPDATE users
      SET
        id = $2,
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
        req.params.username,
        newId,
        newUsername,
        req.body.firstName ?? null,
        req.body.lastName ?? null,
        req.body.email ?? null,
        req.body.password ?? null,
        req.body.phone ?? null,
        req.body.userStatus ?? null,
      ]
    );

    return res.status(200).json(mapUser(result.rows[0]));
  } catch (err) {
    if (err.code === '23505') {
      return res.status(400).json({ message: 'User with this id or username already exists' });
    }
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.delete('/user/:username', async (req, res) => {
  try {
    const result = await pool.query(
      `
      DELETE FROM users
      WHERE username = $1
      `,
      [req.params.username]
    );

    if (result.rowCount === 0) {
      return res.status(404).json({ message: 'User not found' });
    }

    return res.status(200).json({ message: 'successful operation' });
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.use((req, res) => {
  res.status(404).json({ message: 'Not found' });
});

async function start() {
  try {
    await initDb();
    app.listen(PORT, '0.0.0.0', () => {
      console.log(`Petstore API listening on 0.0.0.0:${PORT}`);
    });
  } catch (err) {
    console.error('Failed to initialize application:', err);
    process.exit(1);
  }
}

start();