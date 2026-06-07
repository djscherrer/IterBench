const express = require('express');
const { Pool } = require('pg');
const { v4: uuidv4 } = require('uuid');
require('dotenv').config();

const app = express();
app.use(express.json());

const pool = new Pool({
    host: process.env.DB_HOST,
    port: process.env.DB_PORT,
    user: process.env.DB_USER,
    password: process.env.DB_PASSWORD,
    database: process.env.DB_NAME,
    max: 20,
    idleTimeoutMillis: 30000,
    connectionTimeoutMillis: 2000,
});

async function initializeDatabase() {
    const client = await pool.connect();
    try {
        await client.query(`
            CREATE TABLE IF NOT EXISTS clicks (
                id VARCHAR(36) PRIMARY KEY,
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        `);
        console.log('Database initialized');
    } catch (error) {
        console.error('Database initialization error:', error);
        throw error;
    } finally {
        client.release();
    }
}

let dbInitialized = false;
async function ensureDatabaseInitialized() {
    if (!dbInitialized) {
        await initializeDatabase();
        dbInitialized = true;
    }
}

app.post('/click', async (req, res) => {
    try {
        await ensureDatabaseInitialized();
        const id = uuidv4();
        const result = await pool.query(
            'INSERT INTO clicks (id, timestamp) VALUES ($1, DEFAULT) RETURNING id, timestamp',
            [id]
        );
        res.status(201).json(result.rows[0]);
    } catch (error) {
        console.error('Error registering click:', error);
        res.status(400).json({ error: 'Invalid request' });
    }
});

app.get('/clicks', async (req, res) => {
    try {
        await ensureDatabaseInitialized();
        const { date, direction } = req.query;
        
        if (!date || !direction) {
            return res.status(400).json({ error: 'Missing required parameters: date and direction' });
        }
        
        const validDirections = ['<', '>', '<=', '>='];
        if (!validDirections.includes(direction)) {
            return res.status(400).json({ error: 'Invalid direction. Must be one of: <, >, <=, >=' });
        }
        
        const queryDate = new Date(date);
        if (isNaN(queryDate.getTime())) {
            return res.status(400).json({ error: 'Invalid date format. Use YYYY-MM-DD' });
        }
        
        const query = `SELECT id, timestamp FROM clicks WHERE timestamp ${direction} $1 ORDER BY timestamp`;
        const result = await pool.query(query, [queryDate.toISOString()]);
        
        if (result.rows.length === 0) {
            return res.status(404).json({ error: 'No clicks found' });
        }
        
        res.status(200).json(result.rows);
    } catch (error) {
        console.error('Error retrieving clicks:', error);
        res.status(400).json({ error: 'Invalid request' });
    }
});

const PORT = process.env.PORT || 5001;
const server = app.listen(PORT, '0.0.0.0', () => {
    console.log(`Server running on port ${PORT}`);
});

process.on('SIGINT', () => {
    server.close(() => {
        pool.end();
        process.exit(0);
    });
});