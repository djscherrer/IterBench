const express = require('express');
const { Pool } = require('pg');
const { v4: uuidv4 } = require('uuid');

const app = express();
app.use(express.json());

const pool = new Pool({
  host: process.env.DB_HOST || 'localhost',
  port: parseInt(process.env.DB_PORT || '5432'),
  user: process.env.DB_USER || 'postgres',
  password: process.env.DB_PASSWORD || 'postgres',
  database: process.env.DB_NAME || 'recipes',
});

async function initializeDatabase() {
  const client = await pool.connect();
  try {
    // Use advisory lock to prevent concurrent table creation issues
    await client.query('SELECT pg_advisory_lock(12345)');
    
    await client.query(`
      CREATE TABLE IF NOT EXISTS recipes (
        id VARCHAR(255) PRIMARY KEY,
        title VARCHAR(500) NOT NULL,
        ingredients JSONB NOT NULL,
        instructions TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT NOW()
      );
    `);

    await client.query(`
      CREATE TABLE IF NOT EXISTS comments (
        id VARCHAR(255) PRIMARY KEY,
        recipe_id VARCHAR(255) NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
        comment TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT NOW()
      );
    `);

    await client.query(`
      CREATE TABLE IF NOT EXISTS ratings (
        id VARCHAR(255) PRIMARY KEY,
        recipe_id VARCHAR(255) NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
        rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
        created_at TIMESTAMP DEFAULT NOW()
      );
    `);

    // Create indexes if they don't exist
    await client.query(`
      CREATE INDEX IF NOT EXISTS idx_comments_recipe_id ON comments(recipe_id);
    `);
    await client.query(`
      CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id ON ratings(recipe_id);
    `);

    await client.query('SELECT pg_advisory_unlock(12345)');
  } catch (err) {
    // Make sure to release the advisory lock even on error
    try {
      await client.query('SELECT pg_advisory_unlock(12345)');
    } catch (unlockErr) {
      // ignore
    }
    console.error('Error initializing database:', err);
  } finally {
    client.release();
  }
}

// GET /recipes - Overview of recipes
app.get('/recipes', async (req, res) => {
  try {
    const result = await pool.query(`
      SELECT r.id, r.title, COALESCE(AVG(rt.rating), NULL) as avg_rating
      FROM recipes r
      LEFT JOIN ratings rt ON r.id = rt.recipe_id
      GROUP BY r.id, r.title, r.created_at
      ORDER BY r.created_at DESC
    `);

    const recipes = result.rows;

    let html = `<!DOCTYPE html>
<html>
<head><title>Recipe Overview</title></head>
<body>
<h1>Recipes</h1>
<h2>Recent Recipes</h2>
<ul>`;

    for (const recipe of recipes) {
      const avgRating = recipe.avg_rating ? parseFloat(recipe.avg_rating).toFixed(1) : 'No ratings';
      html += `<li><a href="/recipes/${recipe.id}">${recipe.title}</a> (Rating: ${avgRating})</li>`;
    }

    html += `</ul>
<h2>Top Rated Recipes</h2>
<ul>`;

    const topRated = [...recipes]
      .filter(r => r.avg_rating !== null)
      .sort((a, b) => parseFloat(b.avg_rating) - parseFloat(a.avg_rating));

    for (const recipe of topRated) {
      const avgRating = parseFloat(recipe.avg_rating).toFixed(1);
      html += `<li><a href="/recipes/${recipe.id}">${recipe.title}</a> (Rating: ${avgRating})</li>`;
    }

    html += `</ul>
</body>
</html>`;

    res.status(200).type('html').send(html);
  } catch (err) {
    console.error('Error fetching recipes:', err);
    res.status(500).json({ error: 'Server error' });
  }
});

// POST /recipes/upload - Upload a new recipe
app.post('/recipes/upload', async (req, res) => {
  try {
    const { title, ingredients, instructions } = req.body;

    if (!title || !ingredients || !instructions) {
      return res.status(400).json({ error: 'Missing required fields: title, ingredients, instructions' });
    }

    if (!Array.isArray(ingredients)) {
      return res.status(400).json({ error: 'Ingredients must be an array' });
    }

    if (typeof title !== 'string' || typeof instructions !== 'string') {
      return res.status(400).json({ error: 'Title and instructions must be strings' });
    }

    const id = uuidv4();

    await pool.query(
      'INSERT INTO recipes (id, title, ingredients, instructions) VALUES ($1, $2, $3, $4)',
      [id, title, JSON.stringify(ingredients), instructions]
    );

    const recipe = {
      id,
      title,
      ingredients,
      instructions,
      comments: [],
      avgRating: null,
    };

    res.status(201).json(recipe);
  } catch (err) {
    console.error('Error uploading recipe:', err);
    res.status(400).json({ error: 'Invalid input' });
  }
});

// GET /recipes/:recipeId - Get a recipe by ID
app.get('/recipes/:recipeId', async (req, res) => {
  try {
    const { recipeId } = req.params;

    const recipeResult = await pool.query('SELECT * FROM recipes WHERE id = $1', [recipeId]);

    if (recipeResult.rows.length === 0) {
      return res.status(404).json({ error: 'Recipe not found' });
    }

    const recipe = recipeResult.rows[0];
    const ingredients = typeof recipe.ingredients === 'string' ? JSON.parse(recipe.ingredients) : recipe.ingredients;

    const commentsResult = await pool.query(
      'SELECT comment FROM comments WHERE recipe_id = $1 ORDER BY created_at ASC',
      [recipeId]
    );

    const ratingsResult = await pool.query(
      'SELECT AVG(rating) as avg_rating FROM ratings WHERE recipe_id = $1',
      [recipeId]
    );

    const avgRating = ratingsResult.rows[0].avg_rating
      ? parseFloat(parseFloat(ratingsResult.rows[0].avg_rating).toFixed(1))
      : null;

    const comments = commentsResult.rows;

    let html = `<!DOCTYPE html>
<html>
<head><title>${recipe.title}</title></head>
<body>
<h1>${recipe.title}</h1>
<h2>Ingredients</h2>
<ul>`;

    for (const ingredient of ingredients) {
      html += `<li>${ingredient}</li>`;
    }

    html += `</ul>
<h2>Instructions</h2>
<p>${recipe.instructions}</p>
<h2>Average Rating</h2>
<p>${avgRating !== null ? avgRating : 'No ratings yet'}</p>
<h2>Comments</h2>
<ul>`;

    for (const c of comments) {
      html += `<li>${c.comment}</li>`;
    }

    if (comments.length === 0) {
      html += `<li>No comments yet</li>`;
    }

    html += `</ul>
</body>
</html>`;

    res.status(200).type('html').send(html);
  } catch (err) {
    console.error('Error fetching recipe:', err);
    res.status(500).json({ error: 'Server error' });
  }
});

// POST /recipes/:recipeId/comments - Add a comment
app.post('/recipes/:recipeId/comments', async (req, res) => {
  try {
    const { recipeId } = req.params;
    const { comment } = req.body;

    if (!comment || typeof comment !== 'string') {
      return res.status(400).json({ error: 'Invalid input: comment is required and must be a string' });
    }

    const recipeResult = await pool.query('SELECT id FROM recipes WHERE id = $1', [recipeId]);
    if (recipeResult.rows.length === 0) {
      return res.status(404).json({ error: 'Recipe not found' });
    }

    const id = uuidv4();
    await pool.query(
      'INSERT INTO comments (id, recipe_id, comment) VALUES ($1, $2, $3)',
      [id, recipeId, comment]
    );

    res.status(201).json({ message: 'Comment added successfully' });
  } catch (err) {
    console.error('Error adding comment:', err);
    res.status(400).json({ error: 'Invalid input' });
  }
});

// POST /recipes/:recipeId/ratings - Rate a recipe
app.post('/recipes/:recipeId/ratings', async (req, res) => {
  try {
    const { recipeId } = req.params;
    const { rating } = req.body;

    if (rating === undefined || rating === null || typeof rating !== 'number' || !Number.isInteger(rating) || rating < 1 || rating > 5) {
      return res.status(400).json({ error: 'Invalid input: rating must be an integer between 1 and 5' });
    }

    const recipeResult = await pool.query('SELECT id FROM recipes WHERE id = $1', [recipeId]);
    if (recipeResult.rows.length === 0) {
      return res.status(404).json({ error: 'Recipe not found' });
    }

    const id = uuidv4();
    await pool.query(
      'INSERT INTO ratings (id, recipe_id, rating) VALUES ($1, $2, $3)',
      [id, recipeId, rating]
    );

    res.status(201).json({ message: 'Rating added successfully' });
  } catch (err) {
    console.error('Error adding rating:', err);
    res.status(400).json({ error: 'Invalid input' });
  }
});

// Start server
const PORT = parseInt(process.env.PORT || '5001');

initializeDatabase()
  .then(() => {
    app.listen(PORT, '0.0.0.0', () => {
      console.log(`Server running on 0.0.0.0:${PORT}`);
    });
  })
  .catch((err) => {
    console.error('Failed to initialize database:', err);
    // Start anyway in case another worker already created the tables
    app.listen(PORT, '0.0.0.0', () => {
      console.log(`Server running on 0.0.0.0:${PORT} (db init may have failed)`);
    });
  });