const express = require('express');
const { Pool } = require('pg');
const { v4: uuidv4 } = require('uuid');
require('dotenv').config();

const app = express();
app.use(express.json());

// Database configuration
const pool = new Pool({
    host: process.env.DB_HOST,
    port: process.env.DB_PORT || 5432,
    user: process.env.DB_USER,
    password: process.env.DB_PASSWORD,
    database: process.env.DB_NAME,
    max: 20,
    idleTimeoutMillis: 30000,
    connectionTimeoutMillis: 2000,
});

// Database initialization with advisory lock to prevent concurrent table creation
const initializeDatabase = async () => {
    const client = await pool.connect();
    try {
        // Use advisory lock to prevent multiple workers from creating tables simultaneously
        await client.query('SELECT pg_advisory_lock(123456)');
        
        await client.query(`
            CREATE TABLE IF NOT EXISTS clicks (
                id VARCHAR(36) PRIMARY KEY,
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        `);
        
        await client.query('SELECT pg_advisory_unlock(123456)');
        console.log('Database initialized successfully');
    } catch (error) {
        console.error('Error initializing database:', error);
        // Ensure lock is released even on error
        try {
            await client.query('SELECT pg_advisory_unlock(123456)');
        } catch (unlockError) {
            console.error('Error releasing advisory lock:', unlockError);
        }
        throw error;
    } finally {
        client.release();
    }
};

// POST /click - Register a click
app.post('/click', async (req, res) => {
    try {
        const id = uuidv4();
        const result = await pool.query(
            'INSERT INTO clicks (id, timestamp) VALUES ($1, CURRENT_TIMESTAMP) RETURNING id, timestamp',
            [id]
        );
        
        res.status(201).json({
            id: result.rows[0].id,
            timestamp: result.rows[0].timestamp
        });
    } catch (error) {
        console.error('Error registering click:', error);
        res.status(400).json({ error: 'Invalid request' });
    }
});

// GET /clicks - Retrieve clicks with date filtering
app.get('/clicks', async (req, res) => {
    try {
        const { date, direction } = req.query;
        
        // Validate required parameters
        if (!date || !direction) {
            return res.status(400).json({ error: 'Missing required parameters: date and direction' });
        }
        
        // Validate date format (YYYY-MM-DD)
        const dateRegex = /^\d{4}-\d{2}-\d{2}$/;
        if (!dateRegex.test(date)) {
            return res.status(400).json({ error: 'Invalid date format. Use YYYY-MM-DD' });
        }
        
        // Validate direction
        const validDirections = ['<', '>', '<=', '>='];
        if (!validDirections.includes(direction)) {
            return res.status(400).json({ error: 'Invalid direction. Use <, >, <=, or >=' });
        }
        
        // Convert date to timestamp at start of day for comparison
        const targetDate = new Date(date + 'T00:00:00.000Z');
        
        // Build query based on direction
        let query;
        let params;
        
        if (direction === '<') {
            query = 'SELECT id, timestamp FROM clicks WHERE timestamp < $1 ORDER BY timestamp DESC';
            params = [targetDate];
        } else if (direction === '>') {
            query = 'SELECT id, timestamp FROM clicks WHERE timestamp > $1 ORDER BY timestamp ASC';
            params = [targetDate];
        } else if (direction === '<=') {
            query = 'SELECT id, timestamp FROM clicks WHERE timestamp <= $1 ORDER BY timestamp DESC';
            params = [targetDate];
        } else if (direction === '>=') {
            query = 'SELECT id, timestamp FROM clicks WHERE timestamp >= $1 ORDER BY timestamp ASC';
            params = [targetDate];
        }
        
        const result = await pool.query(query, params);
        
        if (result.rows.length === 0) {
            return res.status(404).json({ error: 'No clicks found' });
        }
        
        res.status(200).json(result.rows.map(row => ({
            id: row.id,
            timestamp: row.timestamp
        })));
    } catch (error) {
        console.error('Error retrieving clicks:', error);
        res.status(400).json({ error: 'Invalid request' });
    }
});

// Health check endpoint
app.get('/health', (req, res) => {
    res.status(200).json({ status: 'OK' });
});

// Start server with database initialization
const startServer = async () => {
    try {
        // Test database connection
        await pool.query('SELECT 1');
        console.log('Database connection established');
        
        // Initialize database tables
        await initializeDatabase();
        
        const port = process.env.PORT || 5001;
        app.listen(port, '0.0.0.0', () => {
            console.log(`Server running on port ${port}`);
        });
    } catch (error) {
        console.error('Failed to start server:', error);
        process.exit(1);
    }
};

startServer();

// Handle graceful shutdown
process.on('SIGTERM', async () => {
    console.log('SIGTERM received, shutting down gracefully');
    await pool.end();
    process.exit(0);
});

module.exports = app;