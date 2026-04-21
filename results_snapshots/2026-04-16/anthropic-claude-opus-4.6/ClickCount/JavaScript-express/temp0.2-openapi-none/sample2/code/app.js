const express = require('express');
const { Pool } = require('pg');
const { v4: uuidv4 } = require('uuid');

const app = express();
app.use(express.json());

const pool = new Pool({
  host: process.env.DB_HOST,
  port: parseInt(process.env.DB_PORT, 10) || 5432,
  user: process.env.DB_USER,
  password: process.env.DB_PASSWORD,
  database: process.env.DB_NAME,
});

async function initializeDatabase() {
  const client = await pool.connect();
  try {
    await client.query(`
      CREATE TABLE IF NOT EXISTS clicks (
        id VARCHAR(36) PRIMARY KEY,
        timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
      );
    `);
  } finally {
    client.release();
  }
}

// POST /click
app.post('/click', async (req, res) => {
  try {
    const id = uuidv4();
    await pool.query(
      'INSERT INTO clicks (id, timestamp) VALUES ($1, NOW())',
      [id]
    );
    res.status(201).json({ message: 'Click registered successfully' });
  } catch (err) {
    console.error('Error registering click:', err);
    res.status(400).json({ error: 'Invalid request' });
  }
});

// GET /clicks
app.get('/clicks', async (req, res) => {
  try {
    const { date, direction } = req.query;

    if (!date || !direction) {
      return res.status(400).json({ error: 'Invalid request. Both date and direction are required.' });
    }

    const allowedDirections = ['<', '>', '<=', '>='];
    if (!allowedDirections.includes(direction)) {
      return res.status(400).json({ error: 'Invalid direction. Must be one of: <, >, <=, >=' });
    }

    // Validate date format
    const parsedDate = new Date(date);
    if (isNaN(parsedDate.getTime())) {
      return res.status(400).json({ error: 'Invalid date format.' });
    }

    const query = `SELECT id, timestamp FROM clicks WHERE timestamp ${direction} $1 ORDER BY timestamp`;
    const result = await pool.query(query, [date]);

    if (result.rows.length === 0) {
      return res.status(404).json({ error: 'No clicks found' });
    }

    const clicks = result.rows.map(row => ({
      id: row.id,
      timestamp: row.timestamp.toISOString(),
    }));

    res.status(200).json(clicks);
  } catch (err) {
    console.error('Error retrieving clicks:', err);
    res.status(400).json({ error: 'Invalid request' });
  }
});

const PORT = parseInt(process.env.PORT, 10) || 5001;

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