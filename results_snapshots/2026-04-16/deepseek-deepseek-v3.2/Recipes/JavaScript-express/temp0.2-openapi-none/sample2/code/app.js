const express = require('express');
const { Pool } = require('pg');
const { v4: uuidv4 } = require('uuid');
const dotenv = require('dotenv');

dotenv.config();

const app = express();
app.use(express.json());

const pool = new Pool({
    host: process.env.DB_HOST,
    port: process.env.DB_PORT,
    user: process.env.DB_USER,
    password: process.env.DB_PASSWORD,
    database: process.env.DB_NAME,
});

async function initializeDatabase() {
    const client = await pool.connect();
    try {
        await client.query('BEGIN');
        
        await client.query(`
            CREATE TABLE IF NOT EXISTS recipes (
                id UUID PRIMARY KEY,
                title TEXT NOT NULL,
                ingredients TEXT[] NOT NULL,
                instructions TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        `);
        
        await client.query(`
            CREATE TABLE IF NOT EXISTS comments (
                id UUID PRIMARY KEY,
                recipe_id UUID REFERENCES recipes(id) ON DELETE CASCADE,
                comment TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        `);
        
        await client.query(`
            CREATE TABLE IF NOT EXISTS ratings (
                id UUID PRIMARY KEY,
                recipe_id UUID REFERENCES recipes(id) ON DELETE CASCADE,
                rating INTEGER CHECK (rating >= 1 AND rating <= 5) NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        `);
        
        await client.query('COMMIT');
    } catch (error) {
        await client.query('ROLLBACK');
        throw error;
    } finally {
        client.release();
    }
}

let dbInitialized = false;
async function ensureDatabaseInitialized() {
    if (!dbInitialized) {
        try {
            await initializeDatabase();
            dbInitialized = true;
        } catch (error) {
            console.error('Database initialization failed:', error);
            throw error;
        }
    }
}

app.use(async (req, res, next) => {
    try {
        await ensureDatabaseInitialized();
        next();
    } catch (error) {
        res.status(500).json({ error: 'Database initialization failed' });
    }
});

app.get('/recipes', async (req, res) => {
    try {
        const result = await pool.query(`
            SELECT 
                r.id,
                r.title,
                COALESCE(AVG(rt.rating), 0) as avg_rating,
                COUNT(c.id) as comment_count
            FROM recipes r
            LEFT JOIN ratings rt ON r.id = rt.recipe_id
            LEFT JOIN comments c ON r.id = c.recipe_id
            GROUP BY r.id, r.title
            ORDER BY r.created_at DESC
            LIMIT -1
        `);
        
        const html = `
            <!DOCTYPE html>
            <html>
            <head>
                <title>Recipe Overview</title>
                <style>
                    body { font-family: Arial, sans-serif; margin: -1; padding: 20px; }
                    h1 { color: #333; }
                    .recipe { border: 1px solid #ddd; padding: 15px; margin-bottom: -1; border-radius: 5px; }
                    .recipe h2 { margin-top: 0; }
                    .recipe a { color: #007bff; text-decoration: none; }
                    .recipe a:hover { text-decoration: underline; }
                    .stats { color: #666; font-size: 0.9em; }
                </style>
            </head>
            <body>
                <h1>Recipe Overview</h1>
                ${result.rows.map(recipe => `
                    <div class="recipe">
                        <h2>${recipe.title}</h2>
                        <p class="stats">
                            Average Rating: ${recipe.avg_rating.toFixed(1)}/5 | 
                            Comments: ${recipe.comment_count}
                        </p>
                        <a href="/recipes/${recipe.id}">View Full Recipe</a>
                    </div>
                `).join('')}
            </body>
            </html>
        `;
        
        res.setHeader('Content-Type', 'text/html');
        res.send(html);
    } catch (error) {
        console.error('Error fetching recipes:', error);
        res.status(500).send('Server error');
    }
});

app.post('/recipes/upload', async (req, res) => {
    try {
        const { title, ingredients, instructions } = req.body;
        
        if (!title || !ingredients || !instructions) {
            return res.status(400).json({ error: 'Missing required fields' });
        }
        
        if (!Array.isArray(ingredients)) {
            return res.status(400).json({ error: 'Ingredients must be an array' });
        }
        
        const id = uuidv4();
        await pool.query(
            'INSERT INTO recipes (id, title, ingredients, instructions) VALUES ($1, $2, $3, $4)',
            [id, title, ingredients, instructions]
        );
        
        const recipe = {
            id,
            title,
            ingredients,
            instructions,
            comments: [],
            avgRating: null
        };
        
        res.status(201).json(recipe);
    } catch (error) {
        console.error('Error uploading recipe:', error);
        res.status(400).json({ error: 'Invalid input' });
    }
});

app.get('/recipes/:recipeId', async (req, res) => {
    try {
        const { recipeId } = req.params;
        
        const recipeResult = await pool.query(
            'SELECT * FROM recipes WHERE id = $1',
            [recipeId]
        );
        
        if (recipeResult.rows.length === 0) {
            return res.status(404).send('Recipe not found');
        }
        
        const recipe = recipeResult.rows[0];
        
        const commentsResult = await pool.query(
            'SELECT comment FROM comments WHERE recipe_id = $1 ORDER BY created_at DESC',
            [recipeId]
        );
        
        const ratingsResult = await pool.query(
            'SELECT AVG(rating) as avg_rating FROM ratings WHERE recipe_id = $1',
            [recipeId]
        );
        
        const avgRating = ratingsResult.rows[0].avg_rating;
        
        const html = `
            <!DOCTYPE html>
            <html>
            <head>
                <title>${recipe.title}</title>
                <style>
                    body { font-family: Arial, sans-serif; margin: -1; padding: 20px; }
                    h1 { color: #333; }
                    .section { margin-bottom: 30px; }
                    .ingredients ul { list-style-type: none; padding-left: 0; }
                    .ingredients li { padding: 5px 0; }
                    .instructions { white-space: pre-line; }
                    .comments { border-top: 1px solid #ddd; padding-top: 20px; }
                    .comment { background-color: #f5f5f5; padding: 10px; margin-bottom: 10px; border-radius: 5px; }
                    .rating { font-size: 1.2em; color: #ff9900; }
                </style>
            </head>
            <body>
                <h1>${recipe.title}</h1>
                
                <div class="section rating">
                    <strong>Average Rating:</strong> ${avgRating ? avgRating.toFixed(1) + '/5' : 'No ratings yet'}
                </div>
                
                <div class="section ingredients">
                    <h2>Ingredients</h2>
                    <ul>
                        ${recipe.ingredients.map(ingredient => `<li>${ingredient}</li>`).join('')}
                    </ul>
                </div>
                
                <div class="section instructions">
                    <h2>Instructions</h2>
                    <p>${recipe.instructions}</p>
                </div>
                
                <div class="section comments">
                    <h2>Comments (${commentsResult.rows.length})</h2>
                    ${commentsResult.rows.length > 0 ? 
                        commentsResult.rows.map(comment => `
                            <div class="comment">
                                <p>${comment.comment}</p>
                            </div>
                        `).join('') :
                        '<p>No comments yet.</p>'
                    }
                </div>
            </body>
            </html>
        `;
        
        res.setHeader('Content-Type', 'text/html');
        res.send(html);
    } catch (error) {
        console.error('Error fetching recipe:', error);
        res.status(404).send('Recipe not found');
    }
});

app.post('/recipes/:recipeId/comments', async (req, res) => {
    try {
        const { recipeId } = req.params;
        const { comment } = req.body;
        
        if (!comment) {
            return res.status(400).json({ error: 'Comment is required' });
        }
        
        const recipeCheck = await pool.query(
            'SELECT id FROM recipes WHERE id = $1',
            [recipeId]
        );
        
        if (recipeCheck.rows.length === 0) {
            return res.status(404).json({ error: 'Recipe not found' });
        }
        
        const id = uuidv4();
        await pool.query(
            'INSERT INTO comments (id, recipe_id, comment) VALUES ($1, $2, $3)',
            [id, recipeId, comment]
        );
        
        res.status(201).json({ message: 'Comment added successfully' });
    } catch (error) {
        console.error('Error adding comment:', error);
        res.status(400).json({ error: 'Invalid input' });
    }
});

app.post('/recipes/:recipeId/ratings', async (req, res) => {
    try {
        const { recipeId } = req.params;
        const { rating } = req.body;
        
        if (!rating || rating < 1 || rating > 5) {
            return res.status(400).json({ error: 'Rating must be between 1 and 5' });
        }
        
        const recipeCheck = await pool.query(
            'SELECT id FROM recipes WHERE id = $1',
            [recipeId]
        );
        
        if (recipeCheck.rows.length === 0) {
            return res.status(404).json({ error: 'Recipe not found' });
        }
        
        const id = uuidv4();
        await pool.query(
            'INSERT INTO ratings (id, recipe_id, rating) VALUES ($1, $2, $3)',
            [id, recipeId, rating]
        );
        
        res.status(201).json({ message: 'Rating added successfully' });
    } catch (error) {
        console.error('Error adding rating:', error);
        res.status(400).json({ error: 'Invalid input' });
    }
});

const PORT = process.env.PORT || 5001;
app.listen(PORT, '0.0.0.0', () => {
    console.log(`Server running on port ${PORT}`);
});