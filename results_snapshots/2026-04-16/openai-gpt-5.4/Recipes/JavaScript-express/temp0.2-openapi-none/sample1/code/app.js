require('dotenv').config();

const express = require('express');
const { Pool } = require('pg');
const { v4: uuidv4 } = require('uuid');

const app = express();

app.use(express.json());

const PORT = parseInt(process.env.PORT || '5001', 10);

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
        instructions TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
      )
    `);

    await client.query(`
      CREATE TABLE IF NOT EXISTS recipe_ingredients (
        id BIGSERIAL PRIMARY KEY,
        recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
        ingredient TEXT NOT NULL
      )
    `);

    await client.query(`
      CREATE TABLE IF NOT EXISTS recipe_comments (
        id BIGSERIAL PRIMARY KEY,
        recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
        comment TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
      )
    `);

    await client.query(`
      CREATE TABLE IF NOT EXISTS recipe_ratings (
        id BIGSERIAL PRIMARY KEY,
        recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
        rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
      )
    `);

    await client.query(`
      CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_recipe_id
      ON recipe_ingredients(recipe_id)
    `);

    await client.query(`
      CREATE INDEX IF NOT EXISTS idx_recipe_comments_recipe_id
      ON recipe_comments(recipe_id)
    `);

    await client.query(`
      CREATE INDEX IF NOT EXISTS idx_recipe_ratings_recipe_id
      ON recipe_ratings(recipe_id)
    `);

    await client.query(`
      CREATE INDEX IF NOT EXISTS idx_recipes_created_at
      ON recipes(created_at DESC)
    `);

    await client.query('COMMIT');
  } catch (error) {
    await client.query('ROLLBACK');
    throw error;
  } finally {
    client.release();
  }
}

async function recipeExists(recipeId) {
  const result = await pool.query('SELECT 1 FROM recipes WHERE id = $1', [recipeId]);
  return result.rowCount > 0;
}

async function getRecipeById(recipeId) {
  const recipeResult = await pool.query(`
    SELECT
      r.id,
      r.title,
      r.instructions,
      r.created_at,
      COALESCE(ROUND(AVG(rr.rating)::numeric, 2), NULL) AS avg_rating
    FROM recipes r
    LEFT JOIN recipe_ratings rr ON rr.recipe_id = r.id
    WHERE r.id = $1
    GROUP BY r.id, r.title, r.instructions, r.created_at
  `, [recipeId]);

  if (recipeResult.rowCount === 0) {
    return null;
  }

  const ingredientsResult = await pool.query(`
    SELECT ingredient
    FROM recipe_ingredients
    WHERE recipe_id = $1
    ORDER BY id ASC
  `, [recipeId]);

  const commentsResult = await pool.query(`
    SELECT comment
    FROM recipe_comments
    WHERE recipe_id = $1
    ORDER BY id ASC
  `, [recipeId]);

  const recipe = recipeResult.rows[0];

  return {
    id: recipe.id,
    title: recipe.title,
    ingredients: ingredientsResult.rows.map((row) => row.ingredient),
    instructions: recipe.instructions,
    comments: commentsResult.rows.map((row) => ({ comment: row.comment })),
    avgRating: recipe.avg_rating === null ? null : Number(recipe.avg_rating),
  };
}

app.get('/recipes', async (req, res) => {
  try {
    const result = await pool.query(`
      SELECT
        r.id,
        r.title,
        r.created_at,
        COALESCE(AVG(rr.rating), 0) AS avg_rating
      FROM recipes r
      LEFT JOIN recipe_ratings rr ON rr.recipe_id = r.id
      GROUP BY r.id, r.title, r.created_at
      ORDER BY r.created_at DESC, avg_rating DESC
      LIMIT 50
    `);

    const recipes = result.rows;

    let html = `
      <!DOCTYPE html>
      <html>
      <head>
        <meta charset="utf-8" />
        <title>Recipe Overview</title>
      </head>
      <body>
        <h1>Recipe Overview</h1>
        <ul>
    `;

    for (const recipe of recipes) {
      html += `
        <li>
          <a href="/recipes/${encodeURIComponent(recipe.id)}">${escapeHtml(recipe.title)}</a>
          - Average Rating: ${recipe.avg_rating ? Number(recipe.avg_rating).toFixed(2) : 'N/A'}
        </li>
      `;
    }

    html += `
        </ul>
      </body>
      </html>
    `;

    res.status(200).type('html').send(html);
  } catch (error) {
    console.error('Error getting recipes overview:', error);
    res.status(500).send('Server error');
  }
});

app.post('/recipes/upload', async (req, res) => {
  const { title, ingredients, instructions } = req.body || {};

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

  const recipeId = uuidv4();
  const client = await pool.connect();

  try {
    await client.query('BEGIN');

    await client.query(`
      INSERT INTO recipes (id, title, instructions)
      VALUES ($1, $2, $3)
    `, [recipeId, title.trim(), instructions.trim()]);

    for (const ingredient of ingredients) {
      await client.query(`
        INSERT INTO recipe_ingredients (recipe_id, ingredient)
        VALUES ($1, $2)
      `, [recipeId, ingredient.trim()]);
    }

    await client.query('COMMIT');

    res.status(201).json({
      id: recipeId,
      title: title.trim(),
      ingredients: ingredients.map((item) => item.trim()),
      instructions: instructions.trim(),
      comments: [],
      avgRating: null,
    });
  } catch (error) {
    await client.query('ROLLBACK');
    console.error('Error uploading recipe:', error);
    res.status(500).json({ error: 'Server error' });
  } finally {
    client.release();
  }
});

app.get('/recipes/:recipeId', async (req, res) => {
  try {
    const recipe = await getRecipeById(req.params.recipeId);

    if (!recipe) {
      return res.status(404).send('Recipe not found');
    }

    let html = `
      <!DOCTYPE html>
      <html>
      <head>
        <meta charset="utf-8" />
        <title>${escapeHtml(recipe.title)}</title>
      </head>
      <body>
        <h1>${escapeHtml(recipe.title)}</h1>
        <p><strong>ID:</strong> ${escapeHtml(recipe.id)}</p>
        <p><strong>Average Rating:</strong> ${recipe.avgRating === null ? 'N/A' : recipe.avgRating.toFixed(2)}</p>

        <h2>Ingredients</h2>
        <ul>
    `;

    for (const ingredient of recipe.ingredients) {
      html += `<li>${escapeHtml(ingredient)}</li>`;
    }

    html += `
        </ul>

        <h2>Instructions</h2>
        <p>${escapeHtml(recipe.instructions)}</p>

        <h2>Comments</h2>
        <ul>
    `;

    for (const comment of recipe.comments) {
      html += `<li>${escapeHtml(comment.comment)}</li>`;
    }

    html += `
        </ul>
      </body>
      </html>
    `;

    res.status(200).type('html').send(html);
  } catch (error) {
    console.error('Error getting recipe:', error);
    res.status(500).send('Server error');
  }
});

app.post('/recipes/:recipeId/comments', async (req, res) => {
  const { recipeId } = req.params;
  const { comment } = req.body || {};

  if (typeof comment !== 'string' || comment.trim() === '') {
    return res.status(400).json({ error: 'Invalid input' });
  }

  try {
    const exists = await recipeExists(recipeId);
    if (!exists) {
      return res.status(404).json({ error: 'Recipe not found' });
    }

    await pool.query(`
      INSERT INTO recipe_comments (recipe_id, comment)
      VALUES ($1, $2)
    `, [recipeId, comment.trim()]);

    res.status(201).json({ message: 'Comment added successfully' });
  } catch (error) {
    console.error('Error adding comment:', error);
    res.status(500).json({ error: 'Server error' });
  }
});

app.post('/recipes/:recipeId/ratings', async (req, res) => {
  const { recipeId } = req.params;
  const { rating } = req.body || {};

  if (!Number.isInteger(rating) || rating < 1 || rating > 5) {
    return res.status(400).json({ error: 'Invalid input' });
  }

  try {
    const exists = await recipeExists(recipeId);
    if (!exists) {
      return res.status(404).json({ error: 'Recipe not found' });
    }

    await pool.query(`
      INSERT INTO recipe_ratings (recipe_id, rating)
      VALUES ($1, $2)
    `, [recipeId, rating]);

    res.status(201).json({ message: 'Rating added successfully' });
  } catch (error) {
    console.error('Error adding rating:', error);
    res.status(500).json({ error: 'Server error' });
  }
});

app.use((req, res) => {
  res.status(404).json({ error: 'Not found' });
});

async function startServer() {
  try {
    await initializeDatabase();
    app.listen(PORT, '0.0.0.0', () => {
      console.log(`Server listening on 0.0.0.0:${PORT}`);
    });
  } catch (error) {
    console.error('Failed to start application:', error);
    process.exit(1);
  }
}

startServer();