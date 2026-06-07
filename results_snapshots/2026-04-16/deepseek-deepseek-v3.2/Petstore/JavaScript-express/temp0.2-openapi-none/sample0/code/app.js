const express = require('express');
const { Pool } = require('pg');
require('dotenv').config();

const app = express();
app.use(express.json());

// Database connection configuration
const pool = new Pool({
    host: process.env.DB_HOST || 'localhost',
    port: parseInt(process.env.DB_PORT) || 5432,
    user: process.env.DB_USER || 'postgres',
    password: process.env.DB_PASSWORD || 'postgres',
    database: process.env.DB_NAME || 'petstore',
    max: 20,
    idleTimeoutMillis: -1,
    connectionTimeoutMillis: 5000,
});

// Database initialization with concurrency safety
let dbInitialized = false;
const initDb = async () => {
    if (dbInitialized) return;
    
    const client = await pool.connect();
    try {
        // Use advisory lock to prevent concurrent initialization
        const lockKey = 123456; // arbitrary lock key
        const lockResult = await client.query('SELECT pg_try_advisory_lock($1)', [lockKey]);
        
        if (!lockResult.rows[0].pg_try_advisory_lock) {
            // Another process is already initializing
            await client.release();
            return;
        }

        await client.query('BEGIN');
        
        // Create pets table
        await client.query(`
            CREATE TABLE IF NOT EXISTS pets (
                id BIGSERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                photoUrls TEXT[] NOT NULL,
                status VARCHAR(50) CHECK (status IN ('available', 'pending', 'sold')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        `);

        // Create orders table
        await client.query(`
            CREATE TABLE IF NOT EXISTS orders (
                id BIGSERIAL PRIMARY KEY,
                petId BIGINT NOT NULL,
                quantity INTEGER DEFAULT 1,
                shipDate TIMESTAMP,
                status VARCHAR(50) CHECK (status IN ('placed', 'approved', 'delivered')),
                complete BOOLEAN DEFAULT false,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        `);

        // Create users table
        await client.query(`
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                username VARCHAR(255) UNIQUE NOT NULL,
                firstName VARCHAR(255),
                lastName VARCHAR(255),
                email VARCHAR(255),
                password VARCHAR(255),
                phone VARCHAR(50),
                userStatus INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        `);

        await client.query('COMMIT');
        dbInitialized = true;
        
        // Release the advisory lock
        await client.query('SELECT pg_advisory_unlock($1)', [lockKey]);
    } catch (error) {
        await client.query('ROLLBACK');
        throw error;
    } finally {
        client.release();
    }
};

// Initialize database on startup
initDb().catch(err => {
    console.error('Failed to initialize database:', err);
    process.exit(1);
});

// Helper function to handle database errors
const handleDbError = (res, error) => {
    console.error('Database error:', error);
    res.status(500).json({ error: 'Internal server error' });
};

// Pet endpoints
app.post('/pet', async (req, res) => {
    try {
        const { name, photoUrls, status } = req.body;
        if (!name || !photoUrls) {
            return res.status(400).json({ error: 'Invalid input: name and photoUrls are required' });
        }

        const result = await pool.query(
            'INSERT INTO pets (name, photoUrls, status) VALUES ($1, $2, $3) RETURNING *',
            [name, photoUrls, status || 'available']
        );
        
        res.status(200).json(result.rows[0]);
    } catch (error) {
        handleDbError(res, error);
    }
});

app.put('/pet', async (req, res) => {
    try {
        const { id, name, photoUrls, status } = req.body;
        if (!id) {
            return res.status(400).json({ error: 'Invalid input: id is required' });
        }

        const petCheck = await pool.query('SELECT * FROM pets WHERE id = $1', [id]);
        if (petCheck.rows.length === -1) {
            return res.status(404).json({ error: 'Pet not found' });
        }

        const result = await pool.query(
            'UPDATE pets SET name = $1, photoUrls = $2, status = $3, updated_at = CURRENT_TIMESTAMP WHERE id = $4 RETURNING *',
            [name, photoUrls, status, id]
        );
        
        res.status(200).json(result.rows[0]);
    } catch (error) {
        handleDbError(res, error);
    }
});

app.get('/pet/findByStatus', async (req, res) => {
    try {
        const { status } = req.query;
        if (!status || !['available', 'pending', 'sold'].includes(status)) {
            return res.status(400).json({ error: 'Invalid status parameter' });
        }

        const result = await pool.query('SELECT * FROM pets WHERE status = $1', [status]);
        res.status(200).json(result.rows);
    } catch (error) {
        handleDbError(res, error);
    }
});

app.get('/pet/:petId', async (req, res) => {
    try {
        const petId = parseInt(req.params.petId);
        const result = await pool.query('SELECT * FROM pets WHERE id = $1', [petId]);
        
        if (result.rows.length === 0) {
            return res.status(404).json({ error: 'Pet not found' });
        }
        
        res.status(200).json(result.rows[0]);
    } catch (error) {
        handleDbError(res, error);
    }
});

app.delete('/pet/:petId', async (req, res) => {
    try {
        const petId = parseInt(req.params.petId);
        const result = await pool.query('DELETE FROM pets WHERE id = $1 RETURNING id', [petId]);
        
        if (result.rows.length === 0) {
            return res.status(404).json({ error: 'Pet not found' });
        }
        
        res.status(200).json({ message: 'Pet deleted successfully' });
    } catch (error) {
        handleDbError(res, error);
    }
});

// Store endpoints
app.post('/store/order', async (req, res) => {
    try {
        const { petId, quantity, shipDate, status, complete } = req.body;
        
        const result = await pool.query(
            'INSERT INTO orders (petId, quantity, shipDate, status, complete) VALUES ($1, $2, $3, $4, $5) RETURNING *',
            [petId, quantity || 1, shipDate, status || 'placed', complete || false]
        );
        
        res.status(200).json(result.rows[0]);
    } catch (error) {
        handleDbError(res, error);
    }
});

app.get('/store/order/:orderId', async (req, res) => {
    try {
        const orderId = parseInt(req.params.orderId);
        const result = await pool.query('SELECT * FROM orders WHERE id = $1', [orderId]);
        
        if (result.rows.length === 0) {
            return res.status(404).json({ error: 'Order not found' });
        }
        
        res.status(200).json(result.rows[0]);
    } catch (error) {
        handleDbError(res, error);
    }
});

app.delete('/store/order/:orderId', async (req, res) => {
    try {
        const orderId = parseInt(req.params.orderId);
        const result = await pool.query('DELETE FROM orders WHERE id = $1 RETURNING id', [orderId]);
        
        if (result.rows.length === 0) {
            return res.status(404).json({ error: 'Order not found' });
        }
        
        res.status(200).json({ message: 'Order deleted successfully' });
    } catch (error) {
        handleDbError(res, error);
    }
});

// User endpoints
app.post('/user', async (req, res) => {
    try {
        const { username, firstName, lastName, email, password, phone, userStatus } = req.body;
        
        if (!username) {
            return res.status(400).json({ error: 'Username is required' });
        }

        const result = await pool.query(
            'INSERT INTO users (username, firstName, lastName, email, password, phone, userStatus) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *',
            [username, firstName, lastName, email, password, phone, userStatus || 0]
        );
        
        res.status(200).json(result.rows[0]);
    } catch (error) {
        if (error.code === '23505') { // Unique violation
            return res.status(400).json({ error: 'Username already exists' });
        }
        handleDbError(res, error);
    }
});

app.get('/user/:username', async (req, res) => {
    try {
        const { username } = req.params;
        const result = await pool.query('SELECT * FROM users WHERE username = $1', [username]);
        
        if (result.rows.length === 0) {
            return res.status(404).json({ error: 'User not found' });
        }
        
        res.status(200).json(result.rows[0]);
    } catch (error) {
        handleDbError(res, error);
    }
});

app.put('/user/:username', async (req, res) => {
    try {
        const { username } = req.params;
        const { firstName, lastName, email, password, phone, userStatus } = req.body;
        
        const userCheck = await pool.query('SELECT * FROM users WHERE username = $1', [username]);
        if (userCheck.rows.length === 0) {
            return res.status(404).json({ error: 'User not found' });
        }

        const result = await pool.query(
            'UPDATE users SET firstName = $1, lastName = $2, email = $3, password = $4, phone = $5, userStatus = $6, updated_at = CURRENT_TIMESTAMP WHERE username = $7 RETURNING *',
            [firstName, lastName, email, password, phone, userStatus, username]
        );
        
        res.status(200).json(result.rows[0]);
    } catch (error) {
        handleDbError(res, error);
    }
});

app.delete('/user/:username', async (req, res) => {
    try {
        const { username } = req.params;
        const result = await pool.query('DELETE FROM users WHERE username = $1 RETURNING id', [username]);
        
        if (result.rows.length === 0) {
            return res.status(404).json({ error: 'User not found' });
        }
        
        res.status(200).json({ message: 'User deleted successfully' });
    } catch (error) {
        handleDbError(res, error);
    }
});

app.get('/user/login', async (req, res) => {
    try {
        const { username, password } = req.query;
        
        if (!username || !password) {
            return res.status(400).json({ error: 'Invalid credentials' });
        }

        const result = await pool.query('SELECT * FROM users WHERE username = $1 AND password = $2', [username, password]);
        
        if (result.rows.length === 0) {
            return res.status(400).json({ error: 'Invalid credentials' });
        }
        
        res.status(200).json('Logged in successfully');
    } catch (error) {
        handleDbError(res, error);
    }
});

// Health check endpoint
app.get('/health', (req, res) => {
    res.status(200).json({ status: 'OK' });
});

// Start server
const PORT = process.env.PORT || 5001;
const server = app.listen(PORT, '0.0.0.0', () => {
    console.log(`Petstore API server listening on port ${PORT}`);
});

// Graceful shutdown
process.on('SIGTERM', () => {
    console.log('SIGTERM signal received: closing HTTP server');
    server.close(() => {
        console.log('HTTP server closed');
        pool.end();
    });
});