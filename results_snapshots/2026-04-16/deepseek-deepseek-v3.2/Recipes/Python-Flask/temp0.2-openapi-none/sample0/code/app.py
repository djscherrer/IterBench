import os
import json
from datetime import datetime
from flask import Flask, request, jsonify, abort
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool

app = Flask(__name__)

# Database connection pool
db_pool = None

def get_db_connection():
    """Get a database connection from the pool."""
    return db_pool.getconn()

def return_db_connection(conn):
    """Return a database connection to the pool."""
    db_pool.putconn(conn)

def init_db():
    """Initialize database tables if they don't exist."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Create recipes table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS recipes (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    ingredients JSONB NOT NULL,
                    instructions TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Create comments table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS comments (
                    id SERIAL PRIMARY KEY,
                    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                    comment TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Create ratings table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ratings (
                    id SERIAL PRIMARY KEY,
                    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                    rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            conn.commit()
    finally:
        return_db_connection(conn)

@app.before_request
def before_first_request():
    """Initialize database connection pool and tables."""
    global db_pool
    if db_pool is None:
        db_pool = psycopg2.pool.SimpleConnectionPool(
            1, 20,
            host=os.environ.get('DB_HOST', 'localhost'),
            port=os.environ.get('DB_PORT', '5432'),
            user=os.environ.get('DB_USER', 'postgres'),
            password=os.environ.get('DB_PASSWORD', ''),
            database=os.environ.get('DB_NAME', 'recipe_db')
        )
        init_db()

@app.route('/recipes', methods=['GET'])
def get_recipes_overview():
    """Get an overview of recipes."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get recent recipes
            cur.execute("""
                SELECT id, title, created_at 
                FROM recipes 
                ORDER BY created_at DESC 
                LIMIT 10
            """)
            recent_recipes = cur.fetchall()
            
            # Get top-rated recipes (average rating >= 4)
            cur.execute("""
                SELECT r.id, r.title, AVG(rt.rating) as avg_rating
                FROM recipes r
                LEFT JOIN ratings rt ON r.id = rt.recipe_id
                GROUP BY r.id, r.title
                HAVING AVG(rt.rating) >= 4
                ORDER BY avg_rating DESC
                LIMIT 10
            """)
            top_recipes = cur.fetchall()
        
        # Generate HTML response
        html = "<html><body>"
        html += "<h1>Recipe Overview</h1>"
        
        html += "<h2>Recent Recipes</h2>"
        html += "<ul>"
        for recipe in recent_recipes:
            html += f'<li><a href="/recipes/{recipe["id"]}">{recipe["title"]}</a> (Posted: {recipe["created_at"].strftime("%Y-%m-%d")})</li>'
        html += "</ul>"
        
        html += "<h2>Top-Rated Recipes</h2>"
        html += "<ul>"
        for recipe in top_recipes:
            avg_rating = recipe["avg_rating"] or 0
            html += f'<li><a href="/recipes/{recipe["id"]}">{recipe["title"]}</a> (Rating: {avg_rating:.1f}/5)</li>'
        html += "</ul>"
        
        html += "</body></html>"
        return html, 200, {'Content-Type': 'text/html'}
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        return_db_connection(conn)

@app.route('/recipes/upload', methods=['POST'])
def upload_recipe():
    """Upload a new recipe."""
    data = request.get_json()
    
    # Validate required fields
    if not data or 'title' not in data or 'ingredients' not in data or 'instructions' not in data:
        abort(400, description="Missing required fields: title, ingredients, instructions")
    
    title = data['title']
    ingredients = data['ingredients']
    instructions = data['instructions']
    
    # Validate types
    if not isinstance(title, str) or not isinstance(ingredients, list) or not isinstance(instructions, str):
        abort(400, description="Invalid data types")
    
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO recipes (title, ingredients, instructions) VALUES (%s, %s, %s) RETURNING id",
                (title, json.dumps(ingredients), instructions)
            )
            recipe_id = cur.fetchone()['id']
            conn.commit()
            
            # Return the created recipe
            return jsonify({
                "id": str(recipe_id),
                "title": title,
                "ingredients": ingredients,
                "instructions": instructions,
                "comments": [],
                "avgRating": None
            }), 201
            
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 400
    finally:
        return_db_connection(conn)

@app.route('/recipes/<int:recipe_id>', methods=['GET'])
def get_recipe(recipe_id):
    """Get a recipe by ID."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get recipe details
            cur.execute("SELECT id, title, ingredients, instructions, created_at FROM recipes WHERE id = %s", (recipe_id,))
            recipe = cur.fetchone()
            
            if not recipe:
                abort(404, description="Recipe not found")
            
            # Get comments
            cur.execute("SELECT id, comment, created_at FROM comments WHERE recipe_id = %s ORDER BY created_at DESC", (recipe_id,))
            comments = cur.fetchall()
            
            # Get average rating
            cur.execute("SELECT AVG(rating) as avg_rating FROM ratings WHERE recipe_id = %s", (recipe_id,))
            rating_result = cur.fetchone()
            avg_rating = float(rating_result['avg_rating']) if rating_result['avg_rating'] else None
            
            # Generate HTML response
            html = "<html><body>"
            html += f"<h1>{recipe['title']}</h1>"
            html += f"<p><strong>Posted:</strong> {recipe['created_at'].strftime('%Y-%m-%d %H:%M:%S')}</p>"
            
            html += "<h2>Ingredients</h2>"
            html += "<ul>"
            for ingredient in json.loads(recipe['ingredients']):
                html += f"<li>{ingredient}</li>"
            html += "</ul>"
            
            html += "<h2>Instructions</h2>"
            html += f"<p>{recipe['instructions'].replace(chr(10), '<br>')}</p>"
            
            html += f"<h2>Rating: {avg_rating or 'No ratings yet'}/5</h2>"
            
            html += "<h2>Comments</h2>"
            if comments:
                html += "<ul>"
                for comment in comments:
                    html += f"<li>{comment['comment']} (Posted: {comment['created_at'].strftime('%Y-%m-%d %H:%M:%S')})</li>"
                html += "</ul>"
            else:
                html += "<p>No comments yet.</p>"
            
            # Add form to post comment
            html += """
            <h3>Add Comment</h3>
            <form id="commentForm">
                <textarea name="comment" rows="4" cols="50" required></textarea><br>
                <button type="submit">Submit Comment</button>
            </form>
            <script>
                document.getElementById('commentForm').addEventListener('submit', async (e) => {
                    e.preventDefault();
                    const comment = e.target.comment.value;
                    const response = await fetch(`/recipes/%s/comments`, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({comment})
                    });
                    if (response.ok) {
                        alert('Comment added successfully!');
                        location.reload();
                    } else {
                        alert('Error adding comment');
                    }
                });
            </script>
            """ % recipe_id
            
            # Add form to add rating
            html += """
            <h3>Rate this Recipe</h3>
            <form id="ratingForm">
                <select name="rating" required>
                    <option value="">Select rating</option>
                    <option value="1">1 - Poor</option>
                    <option value="2">2 - Fair</option>
                    <option value="3">3 - Good</option>
                    <option value="4">4 - Very Good</option>
                    <option value="5">5 - Excellent</option>
                </select><br>
                <button type="submit">Submit Rating</button>
            </form>
            <script>
                document.getElementById('ratingForm').addEventListener('submit', async (e) => {
                    e.preventDefault();
                    const rating = parseInt(e.target.rating.value);
                    const response = await fetch(`/recipes/%s/ratings`, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({rating})
                    });
                    if (response.ok) {
                        alert('Rating added successfully!');
                        location.reload();
                    } else {
                        alert('Error adding rating');
                    }
                });
            </script>
            """ % recipe_id
            
            html += "</body></html>"
            return html, 200, {'Content-Type': 'text/html'}
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        return_db_connection(conn)

@app.route('/recipes/<int:recipe_id>/comments', methods=['POST'])
def add_comment(recipe_id):
    """Add a comment to a recipe."""
    data = request.get_json()
    
    if not data or 'comment' not in data:
        abort(400, description="Missing comment field")
    
    comment = data['comment']
    
    if not isinstance(comment, str):
        abort(400, description="Comment must be a string")
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Check if recipe exists
            cur.execute("SELECT id FROM recipes WHERE id = %s", (recipe_id,))
            if not cur.fetchone():
                abort(404, description="Recipe not found")
            
            # Insert comment
            cur.execute(
                "INSERT INTO comments (recipe_id, comment) VALUES (%s, %s)",
                (recipe_id, comment)
            )
            conn.commit()
            return jsonify({"message": "Comment added successfully"}), 201
            
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 400
    finally:
        return_db_connection(conn)

@app.route('/recipes/<int:recipe_id>/ratings', methods=['POST'])
def add_rating(recipe_id):
    """Rate a recipe."""
    data = request.get_json()
    
    if not data or 'rating' not in data:
        abort(400, description="Missing rating field")
    
    rating = data['rating']
    
    if not isinstance(rating, int) or rating < 1 or rating > 5:
        abort(400, description="Rating must be an integer between 1 and 5")
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Check if recipe exists
            cur.execute("SELECT id FROM recipes WHERE id = %s", (recipe_id,))
            if not cur.fetchone():
                abort(404, description="Recipe not found")
            
            # Insert rating
            cur.execute(
                "INSERT INTO ratings (recipe_id, rating) VALUES (%s, %s)",
                (recipe_id, rating)
            )
            conn.commit()
            return jsonify({"message": "Rating added successfully"}), 201
            
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 400
    finally:
        return_db_connection(conn)

@app.errorhandler(400)
def bad_request(error):
    return jsonify({"error": str(error.description)}), 400

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": str(error.description)}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Internal server error"}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)