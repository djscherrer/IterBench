require('dotenv').config();
const express = require('express');
const { Pool } = require('pg');
const { randomUUID } = require('crypto');

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

const ALLOWED_DIRECTIONS = new Set(['<', '>', '<=', '>=']);

async function initializeDatabase() {
  await pool.query(`
    CREATE TABLE IF NOT EXISTS clicks (
      id TEXT PRIMARY KEY,
      timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
  `);

  await pool.query(`
    CREATE INDEX IF NOT EXISTS idx_clicks_timestamp
    ON clicks (timestamp)
  `);
}

function isValidDateOnly(value) {
  if (typeof value !== 'string') return false;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return false;

  const [year, month, day] = value.split('-').map(Number);
  return (
    date.getUTCFullYear() === year &&
    date.getUTCMonth() + 1 === month &&
    date.getUTCDate() === day
  );
}

app.post('/click', async (req, res) => {
  try {
    const id = typeof randomUUID === 'function' ? randomUUID() : require('uuid').v4();

    const result = await pool.query(
      `
      INSERT INTO clicks (id)
      VALUES ($1)
      RETURNING id, timestamp
      `,
      [id]
    );

    return res.status(201).json(result.rows[0]);
  } catch (error) {
    console.error('Error registering click:', error);
    return res.status(500).json({ error: 'Internal server error' });
  }
});

app.get('/clicks', async (req, res) => {
  try {
    const { date, direction } = req.query;

    if (!date || !direction) {
      return res.status(400).json({ error: 'Missing required query parameters: date and direction' });
    }

    if (!isValidDateOnly(date)) {
      return res.status(400).json({ error: 'Invalid date format. Expected YYYY-MM-DD' });
    }

    if (!ALLOWED_DIRECTIONS.has(direction)) {
      return res.status(400).json({ error: 'Invalid direction. Allowed values are <, >, <=, >=' });
    }

    const sql = `
      SELECT id, timestamp
      FROM clicks
      WHERE timestamp ${direction} $1::date
      ORDER BY timestamp ASC
    `;

    const result = await pool.query(sql, [date]);

    if (result.rows.length === 0) {
      return res.status(404).json({ error: 'No clicks found' });
    }

    return res.status(200).json(result.rows);
  } catch (error) {
    console.error('Error retrieving clicks:', error);
    return res.status(500).json({ error: 'Internal server error' });
  }
});

app.use((req, res) => {
  return res.status(404).json({ error: 'Not found' });
});

async function startServer() {
  try {
    await initializeDatabase();

    app.listen(PORT, '0.0.0.0', () => {
      console.log(`Server listening on 0.0.0.0:${PORT}`);
    });
  } catch (error) {
    console.error('Failed to initialize application:', error);
    process.exit(1);
  }
}

startServer();

process.on('SIGINT', async () => {
  try {
    await pool.end();
  } finally {
    process.exit(0);
  }
});

process.on('SIGTERM', async () => {
  try {
    await pool.end();
  } finally {
    process.exit(0);
  }
});