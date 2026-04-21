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
                    ingredients TEXT[] NOT NULL,
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
    """Initialize database before first request."""
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
                HAVING AVG(rt.rating) >= 4 OR AVG(rt.rating) IS NULL
                ORDER BY avg_rating DESC NULLS LAST
                LIMIT 10
            """)
            top_recipes = cur.fetchall()
        
        # Generate HTML response
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Recipe Overview</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 40px; }
                h1 { color: #333; }
                .section { margin-bottom: 30px; }
                .recipe { padding: 10px; border: 1px solid #ddd; margin: 10px 0; }
                .recipe a { color: #0066cc; text-decoration: none; }
                .recipe a:hover { text-decoration: underline; }
            </style>
        </head>
        <body>
            <h1>Recipe Sharing App</h1>
            
            <div class="section">
                <h2>Recent Recipes</h2>
        """
        
        for recipe in recent_recipes:
            html += f"""
                <div class="recipe">
                    <a href="/recipes/{recipe['id']}">{recipe['title']}</a>
                    <br><small>Posted: {recipe['created_at'].strftime('%Y-%m-%d %H:%M')}</small>
                </div>
            """
        
        html += """
            </div>
            
            <div class="section">
                <h2>Top Rated Recipes</h2>
        """
        
        for recipe in top_recipes:
            avg_rating = recipe['avg_rating']
            rating_display = f"{avg_rating:.1f} stars" if avg_rating else "No ratings yet"
            html += f"""
                <div class="recipe">
                    <a href="/recipes/{recipe['id']}">{recipe['title']}</a>
                    <br><small>{rating_display}</small>
                </div>
            """
        
        html += """
            </div>
        </body>
        </html>
        """
        
        return html, 200, {'Content-Type': 'text/html'}
        
    except Exception as e:
        app.logger.error(f"Error getting recipes overview: {e}")
        abort(500)
    finally:
        return_db_connection(conn)

@app.route('/recipes/upload', methods=['POST'])
def upload_recipe():
    """Upload a new recipe."""
    data = request.get_json()
    
    if not data:
        return jsonify({'error': 'No JSON data provided'}), 400
    
    required_fields = ['title', 'ingredients', 'instructions']
    for field in required_fields:
        if field not in data:
            return jsonify({'error': f'Missing required field: {field}'}), 400
    
    title = data['title']
    ingredients = data['ingredients']
    instructions = data['instructions']
    
    if not isinstance(ingredients, list) or not all(isinstance(i, str) for i in ingredients):
        return jsonify({'error': 'Ingredients must be a list of strings'}), 400
    
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO recipes (title, ingredients, instructions) VALUES (%s, %s, %s) RETURNING id",
                (title, ingredients, instructions)
            )
            recipe_id = cur.fetchone()['id']
            conn.commit()
            
            # Return the created recipe
            recipe = {
                'id': recipe_id,
                'title': title,
                'ingredients': ingredients,
                'instructions': instructions,
                'comments': [],
                'avgRating': None
            }
            
            return jsonify(recipe), 201
            
    except Exception as e:
        conn.rollback()
        app.logger.error(f"Error uploading recipe: {e}")
        abort(500)
    finally:
        return_db_connection(conn)

@app.route('/recipes/<int:recipe_id>', methods=['GET'])
def get_recipe(recipe_id):
    """Get a recipe by ID."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get recipe details
            cur.execute("SELECT * FROM recipes WHERE id = %s", (recipe_id,))
            recipe = cur.fetchone()
            
            if not recipe:
                abort(404)
            
            # Get comments
            cur.execute("SELECT comment FROM comments WHERE recipe_id = %s ORDER BY created_at DESC", (recipe_id,))
            comments = [row['comment'] for row in cur.fetchall()]
            
            # Get average rating
            cur.execute("SELECT AVG(rating) as avg_rating FROM ratings WHERE recipe_id = %s", (recipe_id,))
            avg_rating_result = cur.fetchone()
            avg_rating = float(avg_rating_result['avg_rating']) if avg_rating_result['avg_rating'] else None
            
            # Generate HTML response
            html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>{recipe['title']}</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 40px; }}
                    h1 {{ color: #333; }}
                    .section {{ margin-bottom: 30px; }}
                    .ingredients {{ background: #f5f5f5; padding: 15px; }}
                    .instructions {{ background: #f0f8ff; padding: 15px; }}
                    .comments {{ background: #fff8f0; padding: 15px; }}
                    .comment {{ border-bottom: 1px solid #ddd; padding: 10px 0; }}
                    .rating {{ color: #ff9900; font-weight: bold; }}
                </style>
            </head>
            <body>
                <h1>{recipe['title']}</h1>
                
                <div class="section">
                    <h2>Ingredients</h2>
                    <div class="ingredients">
                        <ul>
            """
            
            for ingredient in recipe['ingredients']:
                html += f"<li>{ingredient}</li>"
            
            html += f"""
                        </ul>
                    </div>
                </div>
                
                <div class="section">
                    <h2>Instructions</h2>
                    <div class="instructions">
                        <p>{recipe['instructions'].replace(chr(10), '<br>')}</p>
                    </div>
                </div>
                
                <div class="section">
                    <h2>Rating</h2>
                    <div class="rating">
            """
            
            if avg_rating:
                html += f"Average Rating: {avg_rating:.1f} stars"
            else:
                html += "No ratings yet"
            
            html += f"""
                    </div>
                </div>
                
                <div class="section">
                    <h2>Comments ({len(comments)})</h2>
                    <div class="comments">
            """
            
            if comments:
                for comment in comments:
                    html += f'<div class="comment">{comment}</div>'
            else:
                html += '<p>No comments yet. Be the first to comment!</p>'
            
            html += """
                    </div>
                </div>
                
                <div class="section">
                    <h3>Add a Comment</h3>
                    <form id="commentForm">
                        <textarea id="commentText" rows="4" cols="50" placeholder="Enter your comment..."></textarea><br>
                        <button type="button" onclick="submitComment()">Submit Comment</button>
                    </form>
                    
                    <h3>Rate this Recipe</h3>
                    <form id="ratingForm">
                        <select id="ratingSelect">
                            <option value="1">1 Star</option>
                            <option value="2">2 Stars</option>
                            <option value="3">3 Stars</option>
                            <option value="4">4 Stars</option>
                            <option value="5" selected>5 Stars</option>
                        </select>
                        <button type="button" onclick="submitRating()">Submit Rating</button>
                    </form>
                </div>
                
                <script>
                    function submitComment() {{
                        const comment = document.getElementById('commentText').value;
                        if (!comment.trim()) {{
                            alert('Please enter a comment');
                            return;
                        }}
                        
                        fetch(`/recipes/{recipe_id}/comments`, {{
                            method: 'POST',
                            headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify({{ comment: comment }})
                        }})
                        .then(response => {{
                            if (response.ok) {{
                                alert('Comment added successfully!');
                                location.reload();
                            }} else {{
                                alert('Error adding comment');
                            }}
                        }});
                    }}
                    
                    function submitRating() {{
                        const rating = document.getElementById('ratingSelect').value;
                        
                        fetch(`/recipes/{recipe_id}/ratings`, {{
                            method: 'POST',
                            headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify({{ rating: parseInt(rating) }})
                        }})
                        .then(response => {{
                            if (response.ok) {{
                                alert('Rating added successfully!');
                                location.reload();
                            }} else {{
                                alert('Error adding rating');
                            }}
                        }});
                    }}
                </script>
            </body>
            </html>
            """
            
            return html, 200, {'Content-Type': 'text/html'}
            
    except Exception as e:
        app.logger.error(f"Error getting recipe: {e}")
        abort(500)
    finally:
        return_db_connection(conn)

@app.route('/recipes/<int:recipe_id>/comments', methods=['POST'])
def add_comment(recipe_id):
    """Add a comment to a recipe."""
    data = request.get_json()
    
    if not data or 'comment' not in data:
        return jsonify({'error': 'Missing comment field'}), 400
    
    comment = data['comment']
    
    if not isinstance(comment, str) or not comment.strip():
        return jsonify({'error': 'Comment must be a non-empty string'}), 400
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Check if recipe exists
            cur.execute("SELECT id FROM recipes WHERE id = %s", (recipe_id,))
            if not cur.fetchone():
                abort(404)
            
            # Insert comment
            cur.execute(
                "INSERT INTO comments (recipe_id, comment) VALUES (%s, %s)",
                (recipe_id, comment.strip())
            )
            conn.commit()
            
            return '', 201
            
    except Exception as e:
        conn.rollback()
        app.logger.error(f"Error adding comment: {e}")
        abort(500)
    finally:
        return_db_connection(conn)

@app.route('/recipes/<int:recipe_id>/ratings', methods=['POST'])
def add_rating(recipe_id):
    """Rate a recipe."""
    data = request.get_json()
    
    if not data or 'rating' not in data:
        return jsonify({'error': 'Missing rating field'}), 400
    
    rating = data['rating']
    
    if not isinstance(rating, int) or rating < 1 or rating > 5:
        return jsonify({'error': 'Rating must be an integer between 1 and 5'}), 400
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Check if recipe exists
            cur.execute("SELECT id FROM recipes WHERE id = %s", (recipe_id,))
            if not cur.fetchone():
                abort(404)
            
            # Insert rating
            cur.execute(
                "INSERT INTO ratings (recipe_id, rating) VALUES (%s, %s)",
                (recipe_id, rating)
            )
            conn.commit()
            
            return '', 201
            
    except Exception as e:
        conn.rollback()
        app.logger.error(f"Error adding rating: {e}")
        abort(500)
    finally:
        return_db_connection(conn)

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)