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
    password: process.env.DB_PASSWORD || '',
    database: process.env.DB_NAME || 'recipe_app',
    max: 20,
    idleTimeoutMillis: 30000,
    connectionTimeoutMillis: 2000,
});

// Database initialization with advisory lock to prevent concurrent initialization
async function initializeDatabase() {
    const client = await pool.connect();
    try {
        // Use advisory lock to ensure only one process initializes the database
        const lockId = 123456; // Arbitrary lock ID
        const lockResult = await client.query('SELECT pg_try_advisory_lock($1)', [lockId]);
        
        if (!lockResult.rows[0].pg_try_advisory_lock) {
            console.log('Another process is initializing the database, skipping...');
            return;
        }

        console.log('Initializing database...');
        
        // Create recipes table
        await client.query(`
            CREATE TABLE IF NOT EXISTS recipes (
                id UUID PRIMARY KEY,
                title TEXT NOT NULL,
                ingredients TEXT[] NOT NULL,
                instructions TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        `);

        // Create comments table
        await client.query(`
            CREATE TABLE IF NOT EXISTS comments (
                id UUID PRIMARY KEY,
                recipe_id UUID REFERENCES recipes(id) ON DELETE CASCADE,
                comment TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        `);

        // Create ratings table
        await client.query(`
            CREATE TABLE IF NOT EXISTS ratings (
                id UUID PRIMARY KEY,
                recipe_id UUID REFERENCES recipes(id) ON DELETE CASCADE,
                rating INTEGER CHECK (rating >= 1 AND rating <= 5) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        `);

        // Create indexes for better performance
        await client.query('CREATE INDEX IF NOT EXISTS idx_comments_recipe_id ON comments(recipe_id)');
        await client.query('CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id ON ratings(recipe_id)');
        
        console.log('Database initialized successfully');
        
        // Release the advisory lock
        await client.query('SELECT pg_advisory_unlock($1)', [lockId]);
    } catch (error) {
        console.error('Error initializing database:', error);
    } finally {
        client.release();
    }
}

// Initialize database on startup
initializeDatabase().catch(console.error);

// Helper function to get recipe with comments and average rating
async function getRecipeWithDetails(recipeId) {
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
            avgRating: ratingResult.rows[0].avg_rating ? parseFloat(ratingResult.rows[0].avg_rating) : null
        };
    } finally {
        client.release();
    }
}

// GET /recipes - Get recipe overview
app.get('/recipes', async (req, res) => {
    try {
        const client = await pool.connect();
        try {
            // Get recent recipes
            const recentRecipes = await client.query(
                'SELECT id, title FROM recipes ORDER BY created_at DESC LIMIT 10'
            );

            // Get top-rated recipes
            const topRatedRecipes = await client.query(`
                SELECT r.id, r.title, COALESCE(AVG(rt.rating), 0) as avg_rating
                FROM recipes r
                LEFT JOIN ratings rt ON r.id = rt.recipe_id
                GROUP BY r.id, r.title
                HAVING COUNT(rt.id) > 0
                ORDER BY avg_rating DESC
                LIMIT 10
            `);

            // Generate HTML response
            const html = `
                <!DOCTYPE html>
                <html>
                <head>
                    <title>Recipe Overview</title>
                    <style>
                        body { font-family: Arial, sans-serif; margin: 40px; }
                        h1 { color: #333; }
                        .section { margin-bottom: 40px; }
                        .recipe-list { list-style-type: none; padding: 0; }
                        .recipe-item { margin: 10px 0; padding: 10px; background: #f5f5f5; border-radius: 5px; }
                        .recipe-link { color: #0066cc; text-decoration: none; }
                        .recipe-link:hover { text-decoration: underline; }
                        .rating { color: #ff9900; font-weight: bold; }
                    </style>
                </head>
                <body>
                    <h1>Recipe Sharing App</h1>
                    
                    <div class="section">
                        <h2>Recent Recipes</h2>
                        <ul class="recipe-list">
                            ${recentRecipes.rows.map(recipe => `
                                <li class="recipe-item">
                                    <a href="/recipes/${recipe.id}" class="recipe-link">${recipe.title}</a>
                                </li>
                            `).join('')}
                        </ul>
                    </div>
                    
                    <div class="section">
                        <h2>Top Rated Recipes</h2>
                        <ul class="recipe-list">
                            ${topRatedRecipes.rows.map(recipe => `
                                <li class="recipe-item">
                                    <a href="/recipes/${recipe.id}" class="recipe-link">${recipe.title}</a>
                                    <span class="rating"> (${parseFloat(recipe.avg_rating).toFixed(1)}★)</span>
                                </li>
                            `).join('')}
                        </ul>
                    </div>
                </body>
                </html>
            `;

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

// POST /recipes/upload - Upload a new recipe
app.post('/recipes/upload', async (req, res) => {
    const { title, ingredients, instructions } = req.body;

    if (!title || !ingredients || !instructions) {
        return res.status(400).json({ error: 'Missing required fields: title, ingredients, instructions' });
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

            const recipe = await getRecipeWithDetails(recipeId);
            res.status(201).json(recipe);
        } finally {
            client.release();
        }
    } catch (error) {
        console.error('Error uploading recipe:', error);
        res.status(500).json({ error: 'Server error' });
    }
});

// GET /recipes/{recipeId} - Get a recipe
app.get('/recipes/:recipeId', async (req, res) => {
    const { recipeId } = req.params;

    try {
        const recipe = await getRecipeWithDetails(recipeId);

        if (!recipe) {
            return res.status(404).send('Recipe not found');
        }

        // Generate HTML response
        const html = `
            <!DOCTYPE html>
            <html>
            <head>
                <title>${recipe.title}</title>
                <style>
                    body { font-family: Arial, sans-serif; margin: 40px; }
                    h1 { color: #333; }
                    .section { margin-bottom: 30px; }
                    .rating { color: #ff9900; font-size: 24px; font-weight: bold; }
                    .ingredients-list, .comments-list { list-style-type: none; padding: 0; }
                    .ingredient-item, .comment-item { 
                        margin: 10px 0; 
                        padding: 10px; 
                        background: #f5f5f5; 
                        border-radius: 5px; 
                    }
                    .instructions { 
                        white-space: pre-line; 
                        line-height: 1.6; 
                        padding: 20px; 
                        background: #f9f9f9; 
                        border-radius: 5px; 
                    }
                    .back-link { 
                        display: inline-block; 
                        margin-top: 20px; 
                        color: #0066cc; 
                        text-decoration: none; 
                    }
                    .back-link:hover { text-decoration: underline; }
                </style>
            </head>
            <body>
                <a href="/recipes" class="back-link">← Back to Recipes</a>
                <h1>${recipe.title}</h1>
                
                <div class="section">
                    <h2>Rating</h2>
                    <div class="rating">
                        ${recipe.avgRating ? `${recipe.avgRating.toFixed(1)}★` : 'No ratings yet'}
                    </div>
                </div>
                
                <div class="section">
                    <h2>Ingredients</h2>
                    <ul class="ingredients-list">
                        ${recipe.ingredients.map(ingredient => `
                            <li class="ingredient-item">${ingredient}</li>
                        `).join('')}
                    </ul>
                </div>
                
                <div class="section">
                    <h2>Instructions</h2>
                    <div class="instructions">${recipe.instructions}</div>
                </div>
                
                <div class="section">
                    <h2>Comments</h2>
                    ${recipe.comments.length > 0 ? `
                        <ul class="comments-list">
                            ${recipe.comments.map(comment => `
                                <li class="comment-item">${comment.comment}</li>
                            `).join('')}
                        </ul>
                    ` : '<p>No comments yet</p>'}
                </div>
                
                <div class="section">
                    <h3>Add a Comment</h3>
                    <form id="commentForm">
                        <textarea id="comment" rows="4" cols="50" placeholder="Enter your comment..." required></textarea><br>
                        <button type="submit">Submit Comment</button>
                    </form>
                </div>
                
                <div class="section">
                    <h3>Rate this Recipe</h3>
                    <form id="ratingForm">
                        <select id="rating" required>
                            <option value="">Select rating</option>
                            <option value="1">1 Star</option>
                            <option value="2">2 Stars</option>
                            <option value="3">3 Stars</option>
                            <option value="4">4 Stars</option>
                            <option value="5">5 Stars</option>
                        </select><br>
                        <button type="submit">Submit Rating</button>
                    </form>
                </div>
                
                <script>
                    document.getElementById('commentForm').addEventListener('submit', async (e) => {
                        e.preventDefault();
                        const comment = document.getElementById('comment').value;
                        
                        const response = await fetch('/recipes/${recipeId}/comments', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ comment })
                        });
                        
                        if (response.ok) {
                            alert('Comment added successfully!');
                            location.reload();
                        } else {
                            alert('Error adding comment');
                        }
                    });
                    
                    document.getElementById('ratingForm').addEventListener('submit', async (e) => {
                        e.preventDefault();
                        const rating = parseInt(document.getElementById('rating').value);
                        
                        const response = await fetch('/recipes/${recipeId}/ratings', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ rating })
                        });
                        
                        if (response.ok) {
                            alert('Rating added successfully!');
                            location.reload();
                        } else {
                            alert('Error adding rating');
                        }
                    });
                </script>
            </body>
            </html>
        `;

        res.setHeader('Content-Type', 'text/html');
        res.status(200).send(html);
    } catch (error) {
        console.error('Error fetching recipe:', error);
        res.status(500).json({ error: 'Server error' });
    }
});

// POST /recipes/{recipeId}/comments - Add a comment
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

// POST /recipes/{recipeId}/ratings - Rate a recipe
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
    console.error(err.stack);
    res.status(500).json({ error: 'Something went wrong!' });
});

// Start server
const PORT = process.env.PORT || 5001;
const server = app.listen(PORT, '0.0.0.0', () => {
    console.log(`Server running on port ${PORT}`);
});

// Handle graceful shutdown
process.on('SIGTERM', () => {
    console.log('SIGTERM signal received: closing HTTP server');
    server.close(() => {
        console.log('HTTP server closed');
        pool.end(() => {
            console.log('Database pool closed');
            process.exit(0);
        });
    });
});