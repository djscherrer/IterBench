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

// Database initialization with advisory lock to prevent concurrent initialization
async function initializeDatabase() {
    const client = await pool.connect();
    try {
        // Use advisory lock to ensure only one process initializes the database
        await client.query('SELECT pg_advisory_lock(123456)');
        
        // Check if tables exist
        const checkTables = await client.query(`
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'pets'
            ) as pets_exists,
            EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'orders'
            ) as orders_exists,
            EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'users'
            ) as users_exists
        `);
        
        const { pets_exists, orders_exists, users_exists } = checkTables.rows[0];
        
        if (!pets_exists) {
            await client.query(`
                CREATE TABLE pets (
                    id BIGSERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    photoUrls TEXT[] NOT NULL,
                    status VARCHAR(50) CHECK (status IN ('available', 'pending', 'sold')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            `);
            console.log('Created pets table');
        }
        
        if (!orders_exists) {
            await client.query(`
                CREATE TABLE orders (
                    id BIGSERIAL PRIMARY KEY,
                    petId BIGINT NOT NULL,
                    quantity INTEGER NOT NULL,
                    shipDate TIMESTAMP,
                    status VARCHAR(50) CHECK (status IN ('placed', 'approved', 'delivered')),
                    complete BOOLEAN DEFAULT false,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            `);
            console.log('Created orders table');
        }
        
        if (!users_exists) {
            await client.query(`
                CREATE TABLE users (
                    id BIGSERIAL PRIMARY KEY,
                    username VARCHAR(255) UNIQUE NOT NULL,
                    firstName VARCHAR(255),
                    lastName VARCHAR(255),
                    email VARCHAR(255),
                    password VARCHAR(255),
                    phone VARCHAR(50),
                    userStatus INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            `);
            console.log('Created users table');
        }
        
        await client.query('SELECT pg_advisory_unlock(123456)');
    } catch (error) {
        console.error('Database initialization error:', error);
        try {
            await client.query('SELECT pg_advisory_unlock(123456)');
        } catch (unlockError) {
            console.error('Failed to unlock advisory lock:', unlockError);
        }
        throw error;
    } finally {
        client.release();
    }
}

// Initialize database on startup
initializeDatabase().catch(console.error);

// Pet endpoints
app.post('/pet', async (req, res) => {
    try {
        const { name, photoUrls, status } = req.body;
        if (!name || !photoUrls || !Array.isArray(photoUrls)) {
            return res.status(400).json({ error: 'Invalid input' });
        }
        
        const result = await pool.query(
            'INSERT INTO pets (name, photoUrls, status) VALUES ($1, $2, $3) RETURNING *',
            [name, photoUrls, status || 'available']
        );
        
        res.json(result.rows[0]);
    } catch (error) {
        console.error('Error adding pet:', error);
        res.status(400).json({ error: 'Invalid input' });
    }
});

app.put('/pet', async (req, res) => {
    try {
        const { id, name, photoUrls, status } = req.body;
        if (!id || !name || !photoUrls || !Array.isArray(photoUrls)) {
            return res.status(400).json({ error: 'Invalid input' });
        }
        
        const checkPet = await pool.query('SELECT * FROM pets WHERE id = $1', [id]);
        if (checkPet.rows.length === 0) {
            return res.status(404).json({ error: 'Pet not found' });
        }
        
        const result = await pool.query(
            'UPDATE pets SET name = $1, photoUrls = $2, status = $3 WHERE id = $4 RETURNING *',
            [name, photoUrls, status || 'available', id]
        );
        
        res.json(result.rows[0]);
    } catch (error) {
        console.error('Error updating pet:', error);
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
        console.error('Error finding pets by status:', error);
        res.status(500).json({ error: 'Internal server error' });
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
        console.error('Error getting pet by ID:', error);
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
        console.error('Error deleting pet:', error);
        res.status(404).json({ error: 'Pet not found' });
    }
});

// Store endpoints
app.post('/store/order', async (req, res) => {
    try {
        const { petId, quantity, shipDate, status, complete } = req.body;
        
        if (!petId || quantity === undefined) {
            return res.status(400).json({ error: 'Invalid input' });
        }
        
        // Check if pet exists
        const petCheck = await pool.query('SELECT * FROM pets WHERE id = $1', [petId]);
        if (petCheck.rows.length === 0) {
            return res.status(400).json({ error: 'Pet not found' });
        }
        
        const result = await pool.query(
            `INSERT INTO orders (petId, quantity, shipDate, status, complete) 
             VALUES ($1, $2, $3, $4, $5) RETURNING *`,
            [petId, quantity, shipDate || new Date().toISOString(), status || 'placed', complete || false]
        );
        
        res.json(result.rows[0]);
    } catch (error) {
        console.error('Error placing order:', error);
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
        console.error('Error getting order by ID:', error);
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
        console.error('Error deleting order:', error);
        res.status(404).json({ error: 'Order not found' });
    }
});

// User endpoints
app.post('/user', async (req, res) => {
    try {
        const { username, firstName, lastName, email, password, phone, userStatus } = req.body;
        
        if (!username) {
            return res.status(400).json({ error: 'Invalid input' });
        }
        
        const result = await pool.query(
            `INSERT INTO users (username, firstName, lastName, email, password, phone, userStatus) 
             VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *`,
            [username, firstName, lastName, email, password, phone, userStatus || 0]
        );
        
        res.json(result.rows[0]);
    } catch (error) {
        console.error('Error creating user:', error);
        if (error.code === '23505') { // Unique violation
            return res.status(400).json({ error: 'Username already exists' });
        }
        res.status(400).json({ error: 'Invalid input' });
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
        console.error('Error getting user by username:', error);
        res.status(404).json({ error: 'User not found' });
    }
});

app.put('/user/:username', async (req, res) => {
    try {
        const { username: pathUsername } = req.params;
        const { username, firstName, lastName, email, password, phone, userStatus } = req.body;
        
        // Check if user exists
        const userCheck = await pool.query('SELECT * FROM users WHERE username = $1', [pathUsername]);
        if (userCheck.rows.length === 0) {
            return res.status(404).json({ error: 'User not found' });
        }
        
        const result = await pool.query(
            `UPDATE users SET 
                username = COALESCE($1, username),
                firstName = COALESCE($2, firstName),
                lastName = COALESCE($3, lastName),
                email = COALESCE($4, email),
                password = COALESCE($5, password),
                phone = COALESCE($6, phone),
                userStatus = COALESCE($7, userStatus)
             WHERE username = $8 RETURNING *`,
            [username, firstName, lastName, email, password, phone, userStatus, pathUsername]
        );
        
        res.json(result.rows[0]);
    } catch (error) {
        console.error('Error updating user:', error);
        if (error.code === '23505') { // Unique violation
            return res.status(400).json({ error: 'Username already exists' });
        }
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
        console.error('Error deleting user:', error);
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
        
        // Return a simple success message as per OpenAPI spec
        res.json('Logged in successfully');
    } catch (error) {
        console.error('Error logging in user:', error);
        res.status(400).json({ error: 'Invalid credentials' });
    }
});

// Error handling middleware
app.use((err, req, res, next) => {
    console.error('Unhandled error:', err);
    res.status(500).json({ error: 'Internal server error' });
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
        process.exit(0);
    });
});

process.on('SIGINT', () => {
    console.log('SIGINT signal received: closing HTTP server');
    server.close(() => {
        console.log('HTTP server closed');
        pool.end();
        process.exit(0);
    });
});

module.exports = app;