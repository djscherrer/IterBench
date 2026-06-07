const express = require('express');
const { Pool } = require('pg');
require('dotenv').config();

const app = express();
app.use(express.json());

// Database configuration
const pool = new Pool({
    host: process.env.DB_HOST || 'localhost',
    port: parseInt(process.env.DB_PORT) || 5432,
    user: process.env.DB_USER || 'postgres',
    password: process.env.DB_PASSWORD || 'postgres',
    database: process.env.DB_NAME || 'petstore',
    max: 20,
    idleTimeoutMillis: 30000,
    connectionTimeoutMillis: 2000,
});

// Database initialization with concurrency safety
const initializeDatabase = async () => {
    const client = await pool.connect();
    try {
        // Use advisory lock to prevent concurrent initialization
        await client.query('SELECT pg_advisory_lock(123456)');
        
        // Check if tables exist
        const result = await client.query(`
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'pets'
            );
        `);
        
        if (!result.rows[0].exists) {
            // Create tables
            await client.query(`
                CREATE TABLE pets (
                    id BIGSERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    photoUrls TEXT[] NOT NULL,
                    status VARCHAR(50) CHECK (status IN ('available', 'pending', 'sold'))
                );
                
                CREATE TABLE orders (
                    id BIGSERIAL PRIMARY KEY,
                    petId BIGINT NOT NULL,
                    quantity INTEGER,
                    shipDate TIMESTAMP,
                    status VARCHAR(50) CHECK (status IN ('placed', 'approved', 'delivered')),
                    complete BOOLEAN DEFAULT false
                );
                
                CREATE TABLE users (
                    id BIGSERIAL PRIMARY KEY,
                    username VARCHAR(255) UNIQUE NOT NULL,
                    firstName VARCHAR(255),
                    lastName VARCHAR(255),
                    email VARCHAR(255),
                    password VARCHAR(255),
                    phone VARCHAR(50),
                    userStatus INTEGER DEFAULT 0
                );
            `);
            console.log('Database tables created successfully');
        }
    } catch (error) {
        console.error('Database initialization error:', error);
    } finally {
        await client.query('SELECT pg_advisory_unlock(123456)');
        client.release();
    }
};

// Initialize database on startup
initializeDatabase().catch(console.error);

// Pet endpoints
app.post('/pet', async (req, res) => {
    try {
        const { name, photoUrls, status } = req.body;
        if (!name || !photoUrls) {
            return res.status(400).json({ error: 'Invalid input' });
        }
        
        const result = await pool.query(
            'INSERT INTO pets (name, photoUrls, status) VALUES ($1, $2, $3) RETURNING *',
            [name, photoUrls, status || 'available']
        );
        
        res.json(result.rows[0]);
    } catch (error) {
        res.status(400).json({ error: 'Invalid input' });
    }
});

app.put('/pet', async (req, res) => {
    try {
        const { id, name, photoUrls, status } = req.body;
        if (!id) {
            return res.status(404).json({ error: 'Pet not found' });
        }
        
        const checkResult = await pool.query('SELECT * FROM pets WHERE id = $1', [id]);
        if (checkResult.rows.length === 0) {
            return res.status(404).json({ error: 'Pet not found' });
        }
        
        const result = await pool.query(
            'UPDATE pets SET name = $1, photoUrls = $2, status = $3 WHERE id = $4 RETURNING *',
            [name, photoUrls, status, id]
        );
        
        res.json(result.rows[0]);
    } catch (error) {
        res.status(404).json({ error: 'Pet not found' });
    }
});

app.get('/pet/findByStatus', async (req, res) => {
    try {
        const { status } = req.query;
        const validStatuses = ['available', 'pending', 'sold'];
        
        if (!status || !validStatuses.includes(status)) {
            return res.status(400).json({ error: 'Invalid status parameter' });
        }
        
        const result = await pool.query('SELECT * FROM pets WHERE status = $1', [status]);
        res.json(result.rows);
    } catch (error) {
        res.status(400).json({ error: 'Invalid status parameter' });
    }
});

app.get('/pet/:petId', async (req, res) => {
    try {
        const { petId } = req.params;
        const result = await pool.query('SELECT * FROM pets WHERE id = $1', [petId]);
        
        if (result.rows.length === 0) {
            return res.status(404).json({ error: 'Pet not found' });
        }
        
        res.json(result.rows[0]);
    } catch (error) {
        res.status(404).json({ error: 'Pet not found' });
    }
});

app.delete('/pet/:petId', async (req, res) => {
    try {
        const { petId } = req.params;
        const result = await pool.query('DELETE FROM pets WHERE id = $1 RETURNING *', [petId]);
        
        if (result.rows.length === 0) {
            return res.status(404).json({ error: 'Pet not found' });
        }
        
        res.json({ message: 'Pet deleted successfully' });
    } catch (error) {
        res.status(404).json({ error: 'Pet not found' });
    }
});

// Store endpoints
app.post('/store/order', async (req, res) => {
    try {
        const { petId, quantity, shipDate, status, complete } = req.body;
        
        const result = await pool.query(
            `INSERT INTO orders (petId, quantity, shipDate, status, complete) 
             VALUES ($1, $2, $3, $4, $5) RETURNING *`,
            [petId, quantity || 1, shipDate, status || 'placed', complete || false]
        );
        
        res.json(result.rows[0]);
    } catch (error) {
        res.status(400).json({ error: 'Invalid input' });
    }
});

app.get('/store/order/:orderId', async (req, res) => {
    try {
        const { orderId } = req.params;
        const result = await pool.query('SELECT * FROM orders WHERE id = $1', [orderId]);
        
        if (result.rows.length === 0) {
            return res.status(404).json({ error: 'Order not found' });
        }
        
        res.json(result.rows[0]);
    } catch (error) {
        res.status(404).json({ error: 'Order not found' });
    }
});

app.delete('/store/order/:orderId', async (req, res) => {
    try {
        const { orderId } = req.params;
        const result = await pool.query('DELETE FROM orders WHERE id = $1 RETURNING *', [orderId]);
        
        if (result.rows.length === 0) {
            return res.status(404).json({ error: 'Order not found' });
        }
        
        res.json({ message: 'Order deleted successfully' });
    } catch (error) {
        res.status(404).json({ error: 'Order not found' });
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
            `INSERT INTO users (username, firstName, lastName, email, password, phone, userStatus) 
             VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *`,
            [username, firstName, lastName, email, password, phone, userStatus || 0]
        );
        
        res.json(result.rows[0]);
    } catch (error) {
        if (error.code === '23505') { // Unique violation
            res.status(400).json({ error: 'Username already exists' });
        } else {
            res.status(400).json({ error: 'Invalid input' });
        }
    }
});

app.get('/user/:username', async (req, res) => {
    try {
        const { username } = req.params;
        const result = await pool.query('SELECT * FROM users WHERE username = $1', [username]);
        
        if (result.rows.length === 0) {
            return res.status(404).json({ error: 'User not found' });
        }
        
        res.json(result.rows[0]);
    } catch (error) {
        res.status(404).json({ error: 'User not found' });
    }
});

app.put('/user/:username', async (req, res) => {
    try {
        const { username } = req.params;
        const { firstName, lastName, email, password, phone, userStatus } = req.body;
        
        const checkResult = await pool.query('SELECT * FROM users WHERE username = $1', [username]);
        if (checkResult.rows.length === 0) {
            return res.status(404).json({ error: 'User not found' });
        }
        
        const result = await pool.query(
            `UPDATE users SET firstName = $1, lastName = $2, email = $3, password = $4, 
             phone = $5, userStatus = $6 WHERE username = $7 RETURNING *`,
            [firstName, lastName, email, password, phone, userStatus, username]
        );
        
        res.json(result.rows[0]);
    } catch (error) {
        res.status(404).json({ error: 'User not found' });
    }
});

app.delete('/user/:username', async (req, res) => {
    try {
        const { username } = req.params;
        const result = await pool.query('DELETE FROM users WHERE username = $1 RETURNING *', [username]);
        
        if (result.rows.length === 0) {
            return res.status(404).json({ error: 'User not found' });
        }
        
        res.json({ message: 'User deleted successfully' });
    } catch (error) {
        res.status(404).json({ error: 'User not found' });
    }
});

app.get('/user/login', async (req, res) => {
    try {
        const { username, password } = req.query;
        
        if (!username || !password) {
            return res.status(400).json({ error: 'Invalid credentials' });
        }
        
        const result = await pool.query(
            'SELECT * FROM users WHERE username = $1 AND password = $2',
            [username, password]
        );
        
        if (result.rows.length === 0) {
            return res.status(400).json({ error: 'Invalid credentials' });
        }
        
        res.json('Logged in successfully');
    } catch (error) {
        res.status(400).json({ error: 'Invalid credentials' });
    }
});

// Health check endpoint
app.get('/health', (req, res) => {
    res.json({ status: 'OK' });
});

// Start server
const PORT = process.env.PORT || 5001;
const HOST = '0.0.0.0';

app.listen(PORT, HOST, () => {
    console.log(`Server running on http://${HOST}:${PORT}`);
});

module.exports = app;