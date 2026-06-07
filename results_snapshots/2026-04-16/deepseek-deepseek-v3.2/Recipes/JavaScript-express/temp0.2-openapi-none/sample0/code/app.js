const express = require('express');
const { Pool } = require('pg');
const { v4: uuidv4 } = require('uuid');
require('dotenv').config();

const app = express();
app.use(express.json());

// Database configuration
const pool = new Pool({
    host: process.env.DB_HOST || 'localhost',
    port: parseInt(process.env.DB_PORT) || 5432,
    user: process.env.DB_USER || 'postgres',
    password: process.env.DB_PASSWORD || 'postgres',
    database: process.env.DB_NAME || 'recipe_app',
    max: 20,
    idleTimeoutMillis: 30000,
    connectionTimeoutMillis: 2000,
});

// Database initialization with advisory lock to prevent concurrent initialization
const initDb = async () => {
    const client = await pool.connect();
    try {
        // Use advisory lock to ensure only one process initializes the database
        await client.query('SELECT pg_advisory_lock(123456)');
        
        // Check if tables exist
        const checkTableQuery = `
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_name = 'recipes'
            );
        `;
        const result = await client.query(checkTableQuery);
        
        if (!result.rows[0].exists) {
            console.log('Initializing database tables...');
            
            await client.query(`
                CREATE TABLE recipes (
                    id UUID PRIMARY KEY,
                    title VARCHAR(255) NOT NULL,
                    ingredients TEXT[] NOT NULL,
                    instructions TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            `);
            
            await client.query(`
                CREATE TABLE comments (
                    id UUID PRIMARY KEY,
                    recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                    comment TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            `);
            
            await client.query(`
                CREATE TABLE ratings (
                    id UUID PRIMARY KEY,
                    recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                    rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            `);
            
            console.log('Database tables created successfully');
        } else {
            console.log('Database tables already exist');
        }
        
        await client.query('SELECT pg_advisory_unlock(123456)');
    } catch (error) {
        console.error('Error initializing database:', error);
        try {
            await client.query('SELECT pg_advisory_unlock(123456)');
        } catch (unlockError) {
            console.error('Error unlocking advisory lock:', unlockError);
        }
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

// Helper function to get recipe with comments and average rating
const getRecipeWithDetails = async (recipeId) => {
    const client = await pool.connect();
    try {
        // Get recipe
        const recipeResult = await client.query(
            'SELECT * FROM recipes WHERE id = $1',
            [recipeId]
        );
        
        if (recipeResult.rows.length === 0) {
            return null;
        }
        
        const recipe = recipeResult.rows[0];
        
        // Get comments
        const commentsResult = await client.query(
            'SELECT comment FROM comments WHERE recipe_id = $1 ORDER BY created_at DESC',
            [recipeId]
        );
        
        // Get average rating
        const ratingResult = await client.query(
            'SELECT AVG(rating) as avg_rating FROM ratings WHERE recipe_id = $1',
            [recipeId]
        );
        
        return {
            id: recipe.id,
            title: recipe.title,
            ingredients: recipe.ingredients,
            instructions: recipe.instructions,
            comments: commentsResult.rows,
            avgRating: ratingResult.rows[0].avg_rating ? 
                parseFloat(ratingResult.rows[0].avg_rating) : null
        };
    } finally {
        client.release();
    }
};

// Generate HTML for recipe overview
const generateOverviewHTML = (recipes) => {
    let html = `
        <!DOCTYPE html>
        <html>
        <head>
            <title>Recipe Overview</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 40px; }
                h1 { color: #333; }
                .recipe { border: 1px solid #ddd; padding: 20px; margin: 20px 0; border-radius: 5px; }
                .recipe h2 { margin-top: 0; }
                .recipe a { color: #0066cc; text-decoration: none; }
                .recipe a:hover { text-decoration: underline; }
            </style>
        </head>
        <body>
            <h1>Recipe Overview</h1>
    `;
    
    if (recipes.length === 0) {
        html += '<p>No recipes found.</p>';
    } else {
        recipes.forEach(recipe => {
            html += `
                <div class="recipe">
                    <h2>${recipe.title}</h2>
                    <p><a href="/recipes/${recipe.id}">View Full Recipe</a></p>
                </div>
            `;
        });
    }
    
    html += `
        </body>
        </html>
    `;
    
    return html;
};

// Generate HTML for individual recipe
const generateRecipeHTML = (recipe) => {
    let html = `
        <!DOCTYPE html>
        <html>
        <head>
            <title>${recipe.title}</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 40px; }
                h1 { color: #333; }
                .section { margin: 30px 0; }
                .ingredients ul { list-style-type: none; padding: 0; }
                .ingredients li { padding: 5px 0; }
                .instructions { white-space: pre-line; }
                .rating { color: #ff9900; font-weight: bold; }
                .comment { border-left: 3px solid #ddd; padding-left: 15px; margin: 10px 0; }
            </style>
        </head>
        <body>
            <h1>${recipe.title}</h1>
            
            <div class="section">
                <h2>Average Rating</h2>
                <p class="rating">${recipe.avgRating !== null ? recipe.avgRating.toFixed(1) + ' / 5' : 'No ratings yet'}</p>
            </div>
            
            <div class="section ingredients">
                <h2>Ingredients</h2>
                <ul>
    `;
    
    recipe.ingredients.forEach(ingredient => {
        html += `<li>${ingredient}</li>`;
    });
    
    html += `
                </ul>
            </div>
            
            <div class="section">
                <h2>Instructions</h2>
                <div class="instructions">${recipe.instructions}</div>
            </div>
            
            <div class="section">
                <h2>Comments</h2>
    `;
    
    if (recipe.comments.length === 0) {
        html += '<p>No comments yet.</p>';
    } else {
        recipe.comments.forEach(comment => {
            html += `<div class="comment">${comment.comment}</div>`;
        });
    }
    
    html += `
            </div>
            
            <div class="section">
                <p><a href="/recipes">Back to Recipe Overview</a></p>
            </div>
        </body>
        </html>
    `;
    
    return html;
};

// Routes
app.get('/recipes', async (req, res) => {
    try {
        const client = await pool.connect();
        try {
            const result = await client.query(
                'SELECT id, title FROM recipes ORDER BY created_at DESC LIMIT 20'
            );
            
            const html = generateOverviewHTML(result.rows);
            res.setHeader('Content-Type', 'text/html');
            res.status(200).send(html);
        } finally {
            client.release();
        }
    } catch (error) {
        console.error('Error fetching recipes:', error);
        res.status(500).json({ error: 'Server error' });
    }
});

app.post('/recipes/upload', async (req, res) => {
    const { title, ingredients, instructions } = req.body;
    
    if (!title || !ingredients || !instructions) {
        return res.status(400).json({ error: 'Missing required fields' });
    }
    
    if (!Array.isArray(ingredients)) {
        return res.status(400).json({ error: 'Ingredients must be an array' });
    }
    
    const recipeId = uuidv4();
    
    try {
        const client = await pool.connect();
        try {
            await client.query(
                'INSERT INTO recipes (id, title, ingredients, instructions) VALUES ($1, $2, $3, $4)',
                [recipeId, title, ingredients, instructions]
            );
            
            const recipe = {
                id: recipeId,
                title,
                ingredients,
                instructions,
                comments: [],
                avgRating: null
            };
            
            res.status(201).json(recipe);
        } finally {
            client.release();
        }
    } catch (error) {
        console.error('Error uploading recipe:', error);
        res.status(500).json({ error: 'Server error' });
    }
});

app.get('/recipes/:recipeId', async (req, res) => {
    const { recipeId } = req.params;
    
    try {
        const recipe = await getRecipeWithDetails(recipeId);
        
        if (!recipe) {
            return res.status(404).json({ error: 'Recipe not found' });
        }
        
        const html = generateRecipeHTML(recipe);
        res.setHeader('Content-Type', 'text/html');
        res.status(200).send(html);
    } catch (error) {
        console.error('Error fetching recipe:', error);
        res.status(500).json({ error: 'Server error' });
    }
});

app.post('/recipes/:recipeId/comments', async (req, res) => {
    const { recipeId } = req.params;
    const { comment } = req.body;
    
    if (!comment || typeof comment !== 'string' || comment.trim() === '') {
        return res.status(400).json({ error: 'Comment is required and must be a non-empty string' });
    }
    
    try {
        const client = await pool.connect();
        try {
            // Check if recipe exists
            const recipeCheck = await client.query(
                'SELECT id FROM recipes WHERE id = $1',
                [recipeId]
            );
            
            if (recipeCheck.rows.length === 0) {
                return res.status(404).json({ error: 'Recipe not found' });
            }
            
            const commentId = uuidv4();
            await client.query(
                'INSERT INTO comments (id, recipe_id, comment) VALUES ($1, $2, $3)',
                [commentId, recipeId, comment.trim()]
            );
            
            res.status(201).json({ message: 'Comment added successfully' });
        } finally {
            client.release();
        }
    } catch (error) {
        console.error('Error adding comment:', error);
        res.status(500).json({ error: 'Server error' });
    }
});

app.post('/recipes/:recipeId/ratings', async (req, res) => {
    const { recipeId } = req.params;
    const { rating } = req.body;
    
    if (typeof rating !== 'number' || rating < 1 || rating > 5 || !Number.isInteger(rating)) {
        return res.status(400).json({ error: 'Rating must be an integer between 1 and 5' });
    }
    
    try {
        const client = await pool.connect();
        try {
            // Check if recipe exists
            const recipeCheck = await client.query(
                'SELECT id FROM recipes WHERE id = $1',
                [recipeId]
            );
            
            if (recipeCheck.rows.length === 0) {
                return res.status(404).json({ error: 'Recipe not found' });
            }
            
            const ratingId = uuidv4();
            await client.query(
                'INSERT INTO ratings (id, recipe_id, rating) VALUES ($1, $2, $3)',
                [ratingId, recipeId, rating]
            );
            
            res.status(201).json({ message: 'Rating added successfully' });
        } finally {
            client.release();
        }
    } catch (error) {
        console.error('Error adding rating:', error);
        res.status(500).json({ error: 'Server error' });
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