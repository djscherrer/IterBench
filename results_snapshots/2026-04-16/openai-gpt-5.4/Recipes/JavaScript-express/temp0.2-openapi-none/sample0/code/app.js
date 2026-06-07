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
      CREATE INDEX IF NOT EXISTS idx_comments_recipe_id
      ON comments(recipe_id)
    `);

    await client.query(`
      CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id
      ON ratings(recipe_id)
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

app.get('/recipes', async (req, res) => {
  try {
    const result = await pool.query(`
      SELECT
        r.id,
        r.title,
        r.created_at,
        AVG(rt.rating)::FLOAT AS avg_rating
      FROM recipes r
      LEFT JOIN ratings rt ON rt.recipe_id = r.id
      GROUP BY r.id, r.title, r.created_at
      ORDER BY r.created_at DESC, r.title ASC
      LIMIT 50
    `);

    const recipeItems = result.rows.map((recipe) => {
      const avgRatingText =
        recipe.avg_rating === null
          ? 'No ratings yet'
          : `Average rating: ${Number(recipe.avg_rating).toFixed(1)}`;

      return `
        <li>
          <a href="/recipes/${encodeURIComponent(recipe.id)}">${escapeHtml(recipe.title)}</a>
          <span> - ${escapeHtml(avgRatingText)}</span>
        </li>
      `;
    }).join('');

    const html = `
      <!DOCTYPE html>
      <html lang="en">
      <head>
        <meta charset="UTF-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <title>Recipe Overview</title>
      </head>
      <body>
        <h1>Recipe Overview</h1>
        <p>Recent and top-rated recipes</p>
        <ul>
          ${recipeItems || '<li>No recipes found.</li>'}
        </ul>
      </body>
      </html>
    `;

    res.status(200).type('html').send(html);
  } catch (error) {
    console.error('Error fetching recipes overview:', error);
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

    const recipeId = uuidv4();

    await pool.query(
      `
      INSERT INTO recipes (id, title, ingredients, instructions)
      VALUES ($1, $2, $3, $4)
      `,
      [
        recipeId,
        title.trim(),
        ingredients.map((item) => item.trim()),
        instructions.trim(),
      ]
    );

    const createdRecipe = await pool.query(
      `
      SELECT
        r.id,
        r.title,
        r.ingredients,
        r.instructions,
        COALESCE(
          (
            SELECT json_agg(json_build_object('comment', c.comment) ORDER BY c.created_at ASC)
            FROM comments c
            WHERE c.recipe_id = r.id
          ),
          '[]'::json
        ) AS comments,
        (
          SELECT AVG(rt.rating)::FLOAT
          FROM ratings rt
          WHERE rt.recipe_id = r.id
        ) AS "avgRating"
      FROM recipes r
      WHERE r.id = $1
      `,
      [recipeId]
    );

    res.status(201).json(createdRecipe.rows[0]);
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
        r.created_at,
        (
          SELECT AVG(rt.rating)::FLOAT
          FROM ratings rt
          WHERE rt.recipe_id = r.id
        ) AS avg_rating
      FROM recipes r
      WHERE r.id = $1
      `,
      [recipeId]
    );

    if (recipeResult.rows.length === 0) {
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

    const commentsHtml = commentsResult.rows.map((row) => {
      return `<li>${escapeHtml(row.comment)}</li>`;
    }).join('');

    const avgRatingText =
      recipe.avg_rating === null
        ? 'No ratings yet'
        : Number(recipe.avg_rating).toFixed(1);

    const ingredientsHtml = (recipe.ingredients || []).map((ingredient) => {
      return `<li>${escapeHtml(ingredient)}</li>`;
    }).join('');

    const html = `
      <!DOCTYPE html>
      <html lang="en">
      <head>
        <meta charset="UTF-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <title>${escapeHtml(recipe.title)}</title>
      </head>
      <body>
        <h1>${escapeHtml(recipe.title)}</h1>
        <h2>Ingredients</h2>
        <ul>
          ${ingredientsHtml}
        </ul>
        <h2>Instructions</h2>
        <p>${escapeHtml(recipe.instructions)}</p>
        <h2>Average Rating</h2>
        <p>${escapeHtml(avgRatingText)}</p>
        <h2>Comments</h2>
        <ul>
          ${commentsHtml || '<li>No comments yet.</li>'}
        </ul>
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

    if (recipeExists.rows.length === 0) {
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

    if (recipeExists.rows.length === 0) {
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

app.use((err, req, res, next) => {
  console.error('Unhandled error:', err);
  res.status(500).send('Server error');
});

initializeDatabase()
  .then(() => {
    app.listen(PORT, '0.0.0.0', () => {
      console.log(`Server listening on 0.0.0.0:${PORT}`);
    });
  })
  .catch((error) => {
    console.error('Failed to initialize database:', error);
    process.exit(1);
  });