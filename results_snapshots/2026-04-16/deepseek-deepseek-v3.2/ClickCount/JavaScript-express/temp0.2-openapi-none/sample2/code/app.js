const express = require('express');
const { Pool } = require('pg');
const { v4: uuidv4 } = require('uuid');
require('dotenv').config();

const app = express();
app.use(express.json());

// Database configuration
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

// Database initialization with concurrency safety
let dbInitialized = false;
const initDb = async () => {
    if (dbInitialized) return;
    
    const client = await pool.connect();
    try {
        // Use advisory lock to prevent concurrent initialization
        await client.query('SELECT pg_advisory_lock(123456)');
        
        // Check if table exists
        const tableCheck = await client.query(`
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'clicks'
            );
        `);
        
        if (!tableCheck.rows[0].exists) {
            await client.query(`
                CREATE TABLE clicks (
                    id VARCHAR(36) PRIMARY KEY,
                    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            `);
            console.log('Created clicks table');
        }
        
        dbInitialized = true;
    } finally {
        await client.query('SELECT pg_advisory_unlock(123456)');
        client.release();
    }
};

// Initialize database on startup
initDb().catch(err => {
    console.error('Failed to initialize database:', err);
    process.exit(1);
});

// POST /click - Register a click
app.post('/click', async (req, res) => {
    try {
        const id = uuidv4();
        await pool.query(
            'INSERT INTO clicks (id, timestamp) VALUES ($1, CURRENT_TIMESTAMP)',
            [id]
        );
        
        res.status(201).json({ 
            message: 'Click registered successfully',
            id: id 
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
        
        // Build query based on direction
        let query;
        let params;
        
        if (direction === '<') {
            query = 'SELECT id, timestamp FROM clicks WHERE timestamp < $1::timestamp ORDER BY timestamp DESC';
            params = [`${date} 00:00:00`];
        } else if (direction === '>') {
            query = 'SELECT id, timestamp FROM clicks WHERE timestamp > $1::timestamp ORDER BY timestamp DESC';
            params = [`${date} 23:59:59`];
        } else if (direction === '<=') {
            query = 'SELECT id, timestamp FROM clicks WHERE timestamp <= $1::timestamp ORDER BY timestamp DESC';
            params = [`${date} 23:59:59`];
        } else if (direction === '>=') {
            query = 'SELECT id, timestamp FROM clicks WHERE timestamp >= $1::timestamp ORDER BY timestamp DESC';
            params = [`${date} 00:00:00`];
        }
        
        const result = await pool.query(query, params);
        
        if (result.rows.length === 0) {
            return res.status(404).json({ error: 'No clicks found' });
        }
        
        // Format timestamps to ISO string
        const clicks = result.rows.map(row => ({
            id: row.id,
            timestamp: row.timestamp.toISOString()
        }));
        
        res.status(200).json(clicks);
    } catch (error) {
        console.error('Error retrieving clicks:', error);
        res.status(400).json({ error: 'Invalid request' });
    }
});

// Health check endpoint
app.get('/health', (req, res) => {
    res.status(200).json({ status: 'OK' });
});

// Error handling middleware
app.use((err, req, res, next) => {
    console.error(err.stack);
    res.status(500).json({ error: 'Internal server error' });
});

// Start server
const PORT = process.env.PORT || 5001;
const server = app.listen(PORT, '0.0.0.0', () => {
    console.log(`Server running on port ${PORT}`);
});

// Graceful shutdown
process.on('SIGTERM', () => {
    console.log('SIGTERM received, shutting down gracefully');
    server.close(() => {
        pool.end();
        console.log('Server closed');
        process.exit(0);
    });
});

module.exports = app;