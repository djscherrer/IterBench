require('dotenv').config();
const express = require('express');
const { Pool } = require('pg');
const { v4: uuidv4 } = require('uuid');

const app = express();

app.use(express.json());

const PORT = parseInt(process.env.PORT, 10) || 5001;

const pool = new Pool({
  host: process.env.DB_HOST,
  port: process.env.DB_PORT ? parseInt(process.env.DB_PORT, 10) : 5432,
  user: process.env.DB_USER,
  password: process.env.DB_PASSWORD,
  database: process.env.DB_NAME,
});

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

async function initializeDatabase() {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    await client.query(`
      CREATE TABLE IF NOT EXISTS recipes (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        ingredients TEXT[] NOT NULL,
        instructions TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
      )
    `);

    await client.query(`
      CREATE TABLE IF NOT EXISTS comments (
        id TEXT PRIMARY KEY,
        recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
        comment TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
      )
    `);

    await client.query(`
      CREATE TABLE IF NOT EXISTS ratings (
        id TEXT PRIMARY KEY,
        recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
        rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
      )
    `);

    await client.query(`
      CREATE INDEX IF NOT EXISTS idx_comments_recipe_id ON comments(recipe_id)
    `);

    await client.query(`
      CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id ON ratings(recipe_id)
    `);

    await client.query(`
      CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes(created_at DESC)
    `);

    await client.query('COMMIT');
  } catch (error) {
    await client.query('ROLLBACK');
    throw error;
  } finally {
    client.release();
  }
}

app.get('/recipes', async (req, res) => {
  try {
    const result = await pool.query(
      `
      SELECT
        r.id,
        r.title,
        COALESCE(AVG(rt.rating), NULL) AS avg_rating,
        r.created_at
      FROM recipes r
      LEFT JOIN ratings rt ON rt.recipe_id = r.id
      GROUP BY r.id, r.title, r.created_at
      ORDER BY r.created_at DESC, r.title ASC
      `
    );

    const recentRecipes = result.rows.slice(0, 10);
    const topRatedRecipes = [...result.rows]
      .filter((row) => row.avg_rating !== null)
      .sort((a, b) => Number(b.avg_rating) - Number(a.avg_rating))
      .slice(0, 10);

    const html = `
      <!DOCTYPE html>
      <html>
        <head>
          <meta charset="utf-8" />
          <title>Recipe Overview</title>
        </head>
        <body>
          <h1>Recipe Overview</h1>

          <h2>Recent Recipes</h2>
          ${
            recentRecipes.length === 0
              ? '<p>No recipes found.</p>'
              : `<ul>${recentRecipes
                  .map(
                    (recipe) =>
                      `<li><a href="/recipes/${encodeURIComponent(recipe.id)}">${escapeHtml(
                        recipe.title
                      )}</a></li>`
                  )
                  .join('')}</ul>`
          }

          <h2>Top Rated Recipes</h2>
          ${
            topRatedRecipes.length === 0
              ? '<p>No rated recipes found.</p>'
              : `<ul>${topRatedRecipes
                  .map(
                    (recipe) =>
                      `<li><a href="/recipes/${encodeURIComponent(recipe.id)}">${escapeHtml(
                        recipe.title
                      )}</a> - Avg Rating: ${Number(recipe.avg_rating).toFixed(2)}</li>`
                  )
                  .join('')}</ul>`
          }
        </body>
      </html>
    `;

    res.status(200).type('html').send(html);
  } catch (error) {
    console.error('Error fetching recipe overview:', error);
    res.status(500).send('Server error');
  }
});

app.post('/recipes/upload', async (req, res) => {
  try {
    const { title, ingredients, instructions } = req.body;

    if (
      typeof title !== 'string' ||
      title.trim() === '' ||
      !Array.isArray(ingredients) ||
      ingredients.length === 0 ||
      ingredients.some((item) => typeof item !== 'string' || item.trim() === '') ||
      typeof instructions !== 'string' ||
      instructions.trim() === ''
    ) {
      return res.status(400).json({ error: 'Invalid input' });
    }

    const recipe = {
      id: uuidv4(),
      title: title.trim(),
      ingredients: ingredients.map((item) => item.trim()),
      instructions: instructions.trim(),
    };

    await pool.query(
      `
      INSERT INTO recipes (id, title, ingredients, instructions)
      VALUES ($1, $2, $3, $4)
      `,
      [recipe.id, recipe.title, recipe.ingredients, recipe.instructions]
    );

    res.status(201).json({
      id: recipe.id,
      title: recipe.title,
      ingredients: recipe.ingredients,
      instructions: recipe.instructions,
      comments: [],
      avgRating: null,
    });
  } catch (error) {
    console.error('Error uploading recipe:', error);
    res.status(400).json({ error: 'Invalid input' });
  }
});

app.get('/recipes/:recipeId', async (req, res) => {
  try {
    const { recipeId } = req.params;

    const recipeResult = await pool.query(
      `
      SELECT
        r.id,
        r.title,
        r.ingredients,
        r.instructions,
        COALESCE(AVG(rt.rating), NULL) AS avg_rating
      FROM recipes r
      LEFT JOIN ratings rt ON rt.recipe_id = r.id
      WHERE r.id = $1
      GROUP BY r.id, r.title, r.ingredients, r.instructions
      `,
      [recipeId]
    );

    if (recipeResult.rowCount === 0) {
      return res.status(404).send('Recipe not found');
    }

    const commentsResult = await pool.query(
      `
      SELECT comment, created_at
      FROM comments
      WHERE recipe_id = $1
      ORDER BY created_at ASC
      `,
      [recipeId]
    );

    const recipe = recipeResult.rows[0];
    const comments = commentsResult.rows;

    const html = `
      <!DOCTYPE html>
      <html>
        <head>
          <meta charset="utf-8" />
          <title>${escapeHtml(recipe.title)}</title>
        </head>
        <body>
          <h1>${escapeHtml(recipe.title)}</h1>

          <h2>Ingredients</h2>
          <ul>
            ${recipe.ingredients.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}
          </ul>

          <h2>Instructions</h2>
          <p>${escapeHtml(recipe.instructions)}</p>

          <h2>Average Rating</h2>
          <p>${recipe.avg_rating === null ? 'No ratings yet' : Number(recipe.avg_rating).toFixed(2)}</p>

          <h2>Comments</h2>
          ${
            comments.length === 0
              ? '<p>No comments yet.</p>'
              : `<ul>${comments
                  .map((item) => `<li>${escapeHtml(item.comment)}</li>`)
                  .join('')}</ul>`
          }
        </body>
      </html>
    `;

    res.status(200).type('html').send(html);
  } catch (error) {
    console.error('Error fetching recipe:', error);
    res.status(500).send('Server error');
  }
});

app.post('/recipes/:recipeId/comments', async (req, res) => {
  try {
    const { recipeId } = req.params;
    const { comment } = req.body;

    if (typeof comment !== 'string' || comment.trim() === '') {
      return res.status(400).json({ error: 'Invalid input' });
    }

    const recipeExists = await pool.query(
      `SELECT id FROM recipes WHERE id = $1`,
      [recipeId]
    );

    if (recipeExists.rowCount === 0) {
      return res.status(404).json({ error: 'Recipe not found' });
    }

    await pool.query(
      `
      INSERT INTO comments (id, recipe_id, comment)
      VALUES ($1, $2, $3)
      `,
      [uuidv4(), recipeId, comment.trim()]
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

    if (!Number.isInteger(rating) || rating < 1 || rating > 5) {
      return res.status(400).json({ error: 'Invalid input' });
    }

    const recipeExists = await pool.query(
      `SELECT id FROM recipes WHERE id = $1`,
      [recipeId]
    );

    if (recipeExists.rowCount === 0) {
      return res.status(404).json({ error: 'Recipe not found' });
    }

    await pool.query(
      `
      INSERT INTO ratings (id, recipe_id, rating)
      VALUES ($1, $2, $3)
      `,
      [uuidv4(), recipeId, rating]
    );

    res.status(201).json({ message: 'Rating added successfully' });
  } catch (error) {
    console.error('Error adding rating:', error);
    res.status(400).json({ error: 'Invalid input' });
  }
});

async function start() {
  try {
    await initializeDatabase();
    app.listen(PORT, '0.0.0.0', () => {
      console.log(`Server listening on 0.0.0.0:${PORT}`);
    });
  } catch (error) {
    console.error('Failed to initialize application:', error);
    process.exit(1);
  }
}

start();