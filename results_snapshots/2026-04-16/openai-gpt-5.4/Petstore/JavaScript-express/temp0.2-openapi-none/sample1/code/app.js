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

async function initDb() {
  await pool.query(`
    CREATE TABLE IF NOT EXISTS pets (
      id BIGINT PRIMARY KEY,
      name TEXT NOT NULL,
      photo_urls TEXT[] NOT NULL,
      status TEXT CHECK (status IN ('available', 'pending', 'sold'))
    )
  `);

  await pool.query(`
    CREATE TABLE IF NOT EXISTS orders (
      id BIGINT PRIMARY KEY,
      pet_id BIGINT,
      quantity INTEGER,
      ship_date TIMESTAMPTZ,
      status TEXT CHECK (status IN ('placed', 'approved', 'delivered')),
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
    CREATE SEQUENCE IF NOT EXISTS pet_id_seq START WITH 1 INCREMENT BY 1
  `);

  await pool.query(`
    CREATE SEQUENCE IF NOT EXISTS order_id_seq START WITH 1 INCREMENT BY 1
  `);

  await pool.query(`
    CREATE SEQUENCE IF NOT EXISTS user_id_seq START WITH 1 INCREMENT BY 1
  `);
}

function petRowToResponse(row) {
  return {
    id: Number(row.id),
    name: row.name,
    photoUrls: row.photo_urls || [],
    status: row.status || null,
  };
}

function orderRowToResponse(row) {
  return {
    id: Number(row.id),
    petId: row.pet_id !== null ? Number(row.pet_id) : null,
    quantity: row.quantity,
    shipDate: row.ship_date ? new Date(row.ship_date).toISOString() : null,
    status: row.status || null,
    complete: row.complete,
  };
}

function userRowToResponse(row) {
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

function isValidPetStatus(status) {
  return ['available', 'pending', 'sold'].includes(status);
}

function isValidOrderStatus(status) {
  return ['placed', 'approved', 'delivered'].includes(status);
}

function validatePetBody(body) {
  if (!body || typeof body !== 'object') {
    return 'Invalid input';
  }
  if (typeof body.name !== 'string' || body.name.trim() === '') {
    return 'Invalid input';
  }
  if (!Array.isArray(body.photoUrls) || !body.photoUrls.every((v) => typeof v === 'string')) {
    return 'Invalid input';
  }
  if (body.status !== undefined && body.status !== null && !isValidPetStatus(body.status)) {
    return 'Invalid input';
  }
  if (body.id !== undefined && body.id !== null && !Number.isInteger(Number(body.id))) {
    return 'Invalid input';
  }
  return null;
}

function validateOrderBody(body) {
  if (!body || typeof body !== 'object') {
    return 'Invalid input';
  }
  if (body.id !== undefined && body.id !== null && !Number.isInteger(Number(body.id))) {
    return 'Invalid input';
  }
  if (body.petId !== undefined && body.petId !== null && !Number.isInteger(Number(body.petId))) {
    return 'Invalid input';
  }
  if (body.quantity !== undefined && body.quantity !== null && !Number.isInteger(Number(body.quantity))) {
    return 'Invalid input';
  }
  if (body.shipDate !== undefined && body.shipDate !== null && Number.isNaN(Date.parse(body.shipDate))) {
    return 'Invalid input';
  }
  if (body.status !== undefined && body.status !== null && !isValidOrderStatus(body.status)) {
    return 'Invalid input';
  }
  if (body.complete !== undefined && body.complete !== null && typeof body.complete !== 'boolean') {
    return 'Invalid input';
  }
  return null;
}

function validateUserBody(body) {
  if (!body || typeof body !== 'object') {
    return 'Invalid input';
  }
  if (body.id !== undefined && body.id !== null && !Number.isInteger(Number(body.id))) {
    return 'Invalid input';
  }
  if (body.username !== undefined && body.username !== null && typeof body.username !== 'string') {
    return 'Invalid input';
  }
  if (body.firstName !== undefined && body.firstName !== null && typeof body.firstName !== 'string') {
    return 'Invalid input';
  }
  if (body.lastName !== undefined && body.lastName !== null && typeof body.lastName !== 'string') {
    return 'Invalid input';
  }
  if (body.email !== undefined && body.email !== null && typeof body.email !== 'string') {
    return 'Invalid input';
  }
  if (body.password !== undefined && body.password !== null && typeof body.password !== 'string') {
    return 'Invalid input';
  }
  if (body.phone !== undefined && body.phone !== null && typeof body.phone !== 'string') {
    return 'Invalid input';
  }
  if (body.userStatus !== undefined && body.userStatus !== null && !Number.isInteger(Number(body.userStatus))) {
    return 'Invalid input';
  }
  return null;
}

app.post('/pet', async (req, res) => {
  const validationError = validatePetBody(req.body);
  if (validationError) {
    return res.status(400).json({ message: validationError });
  }

  try {
    let id = req.body.id;
    if (id === undefined || id === null) {
      const seqResult = await pool.query(`SELECT nextval('pet_id_seq') AS id`);
      id = Number(seqResult.rows[0].id);
    } else {
      id = Number(id);
    }

    const query = `
      INSERT INTO pets (id, name, photo_urls, status)
      VALUES ($1, $2, $3, $4)
      RETURNING id, name, photo_urls, status
    `;
    const values = [id, req.body.name, req.body.photoUrls, req.body.status || null];
    const result = await pool.query(query, values);

    return res.status(200).json(petRowToResponse(result.rows[0]));
  } catch (err) {
    if (err.code === '23505') {
      return res.status(400).json({ message: 'Invalid input' });
    }
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.put('/pet', async (req, res) => {
  const validationError = validatePetBody(req.body);
  if (validationError || req.body.id === undefined || req.body.id === null) {
    return res.status(400).json({ message: 'Invalid input' });
  }

  try {
    const query = `
      UPDATE pets
      SET name = $2, photo_urls = $3, status = $4
      WHERE id = $1
      RETURNING id, name, photo_urls, status
    `;
    const values = [Number(req.body.id), req.body.name, req.body.photoUrls, req.body.status || null];
    const result = await pool.query(query, values);

    if (result.rowCount === 0) {
      return res.status(404).json({ message: 'Pet not found' });
    }

    return res.status(200).json(petRowToResponse(result.rows[0]));
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.get('/pet/findByStatus', async (req, res) => {
  const { status } = req.query;
  if (!isValidPetStatus(status)) {
    return res.status(400).json({ message: 'Invalid status' });
  }

  try {
    const result = await pool.query(
      `SELECT id, name, photo_urls, status FROM pets WHERE status = $1 ORDER BY id ASC`,
      [status]
    );
    return res.status(200).json(result.rows.map(petRowToResponse));
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.get('/pet/:petId', async (req, res) => {
  const petId = Number(req.params.petId);
  if (!Number.isInteger(petId)) {
    return res.status(404).json({ message: 'Pet not found' });
  }

  try {
    const result = await pool.query(
      `SELECT id, name, photo_urls, status FROM pets WHERE id = $1`,
      [petId]
    );

    if (result.rowCount === 0) {
      return res.status(404).json({ message: 'Pet not found' });
    }

    return res.status(200).json(petRowToResponse(result.rows[0]));
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.delete('/pet/:petId', async (req, res) => {
  const petId = Number(req.params.petId);
  if (!Number.isInteger(petId)) {
    return res.status(404).json({ message: 'Pet not found' });
  }

  try {
    const result = await pool.query(`DELETE FROM pets WHERE id = $1`, [petId]);
    if (result.rowCount === 0) {
      return res.status(404).json({ message: 'Pet not found' });
    }
    return res.status(200).json({ message: 'successful operation' });
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.post('/store/order', async (req, res) => {
  const validationError = validateOrderBody(req.body);
  if (validationError) {
    return res.status(400).json({ message: validationError });
  }

  try {
    let id = req.body.id;
    if (id === undefined || id === null) {
      const seqResult = await pool.query(`SELECT nextval('order_id_seq') AS id`);
      id = Number(seqResult.rows[0].id);
    } else {
      id = Number(id);
    }

    const query = `
      INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete)
      VALUES ($1, $2, $3, $4, $5, $6)
      RETURNING id, pet_id, quantity, ship_date, status, complete
    `;
    const values = [
      id,
      req.body.petId !== undefined ? Number(req.body.petId) : null,
      req.body.quantity !== undefined ? Number(req.body.quantity) : null,
      req.body.shipDate ? new Date(req.body.shipDate).toISOString() : null,
      req.body.status || null,
      req.body.complete !== undefined ? req.body.complete : null,
    ];
    const result = await pool.query(query, values);

    return res.status(200).json(orderRowToResponse(result.rows[0]));
  } catch (err) {
    if (err.code === '23505') {
      return res.status(400).json({ message: 'Invalid input' });
    }
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.get('/store/order/:orderId', async (req, res) => {
  const orderId = Number(req.params.orderId);
  if (!Number.isInteger(orderId)) {
    return res.status(404).json({ message: 'Order not found' });
  }

  try {
    const result = await pool.query(
      `SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id = $1`,
      [orderId]
    );

    if (result.rowCount === 0) {
      return res.status(404).json({ message: 'Order not found' });
    }

    return res.status(200).json(orderRowToResponse(result.rows[0]));
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.delete('/store/order/:orderId', async (req, res) => {
  const orderId = Number(req.params.orderId);
  if (!Number.isInteger(orderId)) {
    return res.status(404).json({ message: 'Order not found' });
  }

  try {
    const result = await pool.query(`DELETE FROM orders WHERE id = $1`, [orderId]);
    if (result.rowCount === 0) {
      return res.status(404).json({ message: 'Order not found' });
    }
    return res.status(200).json({ message: 'successful operation' });
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.post('/user', async (req, res) => {
  const validationError = validateUserBody(req.body);
  if (validationError || typeof req.body.username !== 'string' || req.body.username.trim() === '') {
    return res.status(400).json({ message: 'Invalid input' });
  }

  try {
    let id = req.body.id;
    if (id === undefined || id === null) {
      const seqResult = await pool.query(`SELECT nextval('user_id_seq') AS id`);
      id = Number(seqResult.rows[0].id);
    } else {
      id = Number(id);
    }

    const query = `
      INSERT INTO users (id, username, first_name, last_name, email, password, phone, user_status)
      VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
      RETURNING id, username, first_name, last_name, email, password, phone, user_status
    `;
    const values = [
      id,
      req.body.username,
      req.body.firstName || null,
      req.body.lastName || null,
      req.body.email || null,
      req.body.password || null,
      req.body.phone || null,
      req.body.userStatus !== undefined ? Number(req.body.userStatus) : null,
    ];
    const result = await pool.query(query, values);

    return res.status(200).json(userRowToResponse(result.rows[0]));
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

    if (result.rowCount === 0) {
      return res.status(400).json({ message: 'Invalid credentials' });
    }

    return res.status(200).json('logged in user session');
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.get('/user/:username', async (req, res) => {
  try {
    const result = await pool.query(
      `SELECT id, username, first_name, last_name, email, password, phone, user_status
       FROM users
       WHERE username = $1`,
      [req.params.username]
    );

    if (result.rowCount === 0) {
      return res.status(404).json({ message: 'User not found' });
    }

    return res.status(200).json(userRowToResponse(result.rows[0]));
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.put('/user/:username', async (req, res) => {
  const validationError = validateUserBody(req.body);
  if (validationError || typeof req.body.username !== 'string' || req.body.username.trim() === '') {
    return res.status(400).json({ message: 'Invalid input' });
  }

  try {
    const existing = await pool.query(`SELECT id FROM users WHERE username = $1`, [req.params.username]);
    if (existing.rowCount === 0) {
      return res.status(404).json({ message: 'User not found' });
    }

    const id = req.body.id !== undefined && req.body.id !== null
      ? Number(req.body.id)
      : Number(existing.rows[0].id);

    const query = `
      UPDATE users
      SET id = $2,
          username = $3,
          first_name = $4,
          last_name = $5,
          email = $6,
          password = $7,
          phone = $8,
          user_status = $9
      WHERE username = $1
      RETURNING id, username, first_name, last_name, email, password, phone, user_status
    `;
    const values = [
      req.params.username,
      id,
      req.body.username,
      req.body.firstName || null,
      req.body.lastName || null,
      req.body.email || null,
      req.body.password || null,
      req.body.phone || null,
      req.body.userStatus !== undefined ? Number(req.body.userStatus) : null,
    ];
    const result = await pool.query(query, values);

    return res.status(200).json(userRowToResponse(result.rows[0]));
  } catch (err) {
    if (err.code === '23505') {
      return res.status(400).json({ message: 'Invalid input' });
    }
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.delete('/user/:username', async (req, res) => {
  try {
    const result = await pool.query(`DELETE FROM users WHERE username = $1`, [req.params.username]);
    if (result.rowCount === 0) {
      return res.status(404).json({ message: 'User not found' });
    }
    return res.status(200).json({ message: 'successful operation' });
  } catch (err) {
    return res.status(500).json({ message: 'Internal server error' });
  }
});

app.use((err, req, res, next) => {
  return res.status(500).json({ message: 'Internal server error' });
});

async function start() {
  try {
    await initDb();
    app.listen(PORT, '0.0.0.0', () => {
      console.log(`Petstore API listening on 0.0.0.0:${PORT}`);
    });
  } catch (err) {
    console.error('Failed to start application:', err);
    process.exit(1);
  }
}

start();