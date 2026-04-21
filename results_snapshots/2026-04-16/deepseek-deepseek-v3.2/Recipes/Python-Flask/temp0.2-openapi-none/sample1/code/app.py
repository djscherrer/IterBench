import os
import json
from datetime import datetime
from flask import Flask, request, jsonify, abort
import psycopg2
from psycopg2.extras import RealDictCursor
import psycopg2.pool

app = Flask(__name__)

# Database connection pool
db_pool = None

def get_db_connection():
    """Get a database connection from the pool."""
    return db_pool.getconn()

def release_db_connection(conn):
    """Release a database connection back to the pool."""
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
                    title VARCHAR(255) NOT NULL,
                    ingredients TEXT NOT NULL,
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
        release_db_connection(conn)

@app.before_request
def before_first_request():
    """Initialize database before first request."""
    if not hasattr(app, 'db_initialized'):
        init_db()
        app.db_initialized = True

def create_app():
    """Application factory function."""
    global db_pool
    
    # Database configuration from environment variables
    db_config = {
        'host': os.environ.get('DB_HOST', 'localhost'),
        'port': os.environ.get('DB_PORT', '5432'),
        'user': os.environ.get('DB_USER', 'postgres'),
        'password': os.environ.get('DB_PASSWORD', ''),
        'database': os.environ.get('DB_NAME', 'recipe_db')
    }
    
    # Create connection pool
    db_pool = psycopg2.pool.SimpleConnectionPool(
        minconn=1,
        maxconn=20,
        **db_config
    )
    
    return app

@app.route('/recipes', methods=['GET'])
def get_recipes_overview():
    """Get an overview of recipes."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get recent recipes
            cur.execute("""
                SELECT r.id, r.title, r.created_at,
                       COALESCE(AVG(rat.rating), 0) as avg_rating,
                       COUNT(DISTINCT c.id) as comment_count
                FROM recipes r
                LEFT JOIN ratings rat ON r.id = rat.recipe_id
                LEFT JOIN comments c ON r.id = c.recipe_id
                GROUP BY r.id
                ORDER BY r.created_at DESC
                LIMIT 10
            """)
            recent_recipes = cur.fetchall()
            
            # Get top-rated recipes
            cur.execute("""
                SELECT r.id, r.title, r.created_at,
                       COALESCE(AVG(rat.rating), 0) as avg_rating,
                       COUNT(DISTINCT c.id) as comment_count
                FROM recipes r
                LEFT JOIN ratings rat ON r.id = rat.recipe_id
                LEFT JOIN comments c ON r.id = c.recipe_id
                GROUP BY r.id
                HAVING COUNT(rat.id) >= 1
                ORDER BY avg_rating DESC
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
                .section { margin-bottom: 40px; }
                .recipe { border: 1px solid #ddd; padding: 15px; margin: 10px 0; border-radius: 5px; }
                .recipe h3 { margin-top: 0; }
                .rating { color: #ff9900; }
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
                    <h3><a href="/recipes/{recipe['id']}">{recipe['title']}</a></h3>
                    <p>Posted: {recipe['created_at'].strftime('%Y-%m-%d %H:%M')}</p>
                    <p class="rating">Average Rating: {recipe['avg_rating']:.1f}/5</p>
                    <p>Comments: {recipe['comment_count']}</p>
                </div>
            """
        
        html += """
            </div>
            
            <div class="section">
                <h2>Top-Rated Recipes</h2>
        """
        
        for recipe in top_recipes:
            html += f"""
                <div class="recipe">
                    <h3><a href="/recipes/{recipe['id']}">{recipe['title']}</a></h3>
                    <p>Posted: {recipe['created_at'].strftime('%Y-%m-%d %H:%M')}</p>
                    <p class="rating">Average Rating: {recipe['avg_rating']:.1f}/5</p>
                    <p>Comments: {recipe['comment_count']}</p>
                </div>
            """
        
        html += """
            </div>
        </body>
        </html>
        """
        
        return html, 200, {'Content-Type': 'text/html'}
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        release_db_connection(conn)

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
    
    # Validate ingredients is a list
    if not isinstance(ingredients, list):
        return jsonify({'error': 'Ingredients must be a list'}), 400
    
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO recipes (title, ingredients, instructions)
                VALUES (%s, %s, %s)
                RETURNING id, title, ingredients, instructions, created_at
            """, (title, json.dumps(ingredients), instructions))
            
            recipe = cur.fetchone()
            conn.commit()
            
            # Convert ingredients back from JSON string
            recipe['ingredients'] = json.loads(recipe['ingredients'])
            
            return jsonify({
                'id': recipe['id'],
                'title': recipe['title'],
                'ingredients': recipe['ingredients'],
                'instructions': recipe['instructions'],
                'comments': [],
                'avgRating': None
            }), 201
            
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        release_db_connection(conn)

@app.route('/recipes/<int:recipe_id>', methods=['GET'])
def get_recipe(recipe_id):
    """Get a recipe by its ID."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get recipe details
            cur.execute("""
                SELECT id, title, ingredients, instructions, created_at
                FROM recipes
                WHERE id = %s
            """, (recipe_id,))
            
            recipe = cur.fetchone()
            
            if not recipe:
                abort(404, description="Recipe not found")
            
            # Get comments
            cur.execute("""
                SELECT id, comment, created_at
                FROM comments
                WHERE recipe_id = %s
                ORDER BY created_at DESC
            """, (recipe_id,))
            
            comments = cur.fetchall()
            
            # Get average rating
            cur.execute("""
                SELECT COALESCE(AVG(rating), 0) as avg_rating
                FROM ratings
                WHERE recipe_id = %s
            """, (recipe_id,))
            
            rating_result = cur.fetchone()
            avg_rating = float(rating_result['avg_rating']) if rating_result['avg_rating'] else 0
            
            # Convert ingredients from JSON string
            ingredients = json.loads(recipe['ingredients'])
            
            # Generate HTML response
            html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>{recipe['title']}</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 40px; }}
                    .recipe {{ border: 1px solid #ddd; padding: 20px; border-radius: 5px; }}
                    .ingredients {{ margin: 20px 0; }}
                    .instructions {{ margin: 20px 0; }}
                    .comments {{ margin-top: 30px; }}
                    .comment {{ border: 1px solid #eee; padding: 10px; margin: 10px 0; }}
                    .rating {{ color: #ff9900; font-size: 1.2em; }}
                </style>
            </head>
            <body>
                <h1>{recipe['title']}</h1>
                <p>Posted: {recipe['created_at'].strftime('%Y-%m-%d %H:%M')}</p>
                
                <div class="rating">
                    <h3>Average Rating: {avg_rating:.1f}/5</h3>
                </div>
                
                <div class="ingredients">
                    <h3>Ingredients:</h3>
                    <ul>
            """
            
            for ingredient in ingredients:
                html += f"<li>{ingredient}</li>"
            
            html += f"""
                    </ul>
                </div>
                
                <div class="instructions">
                    <h3>Instructions:</h3>
                    <p>{recipe['instructions'].replace(chr(10), '<br>')}</p>
                </div>
                
                <div class="comments">
                    <h3>Comments ({len(comments)}):</h3>
            """
            
            if comments:
                for comment in comments:
                    html += f"""
                    <div class="comment">
                        <p>{comment['comment']}</p>
                        <small>Posted: {comment['created_at'].strftime('%Y-%m-%d %H:%M')}</small>
                    </div>
                    """
            else:
                html += "<p>No comments yet. Be the first to comment!</p>"
            
            html += """
                </div>
                
                <div style="margin-top: 30px;">
                    <h3>Add a Comment:</h3>
                    <form id="commentForm">
                        <textarea name="comment" rows="4" cols="50" placeholder="Enter your comment..."></textarea><br>
                        <button type="submit">Submit Comment</button>
                    </form>
                    
                    <h3>Rate this Recipe:</h3>
                    <form id="ratingForm">
                        <select name="rating">
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
                    document.getElementById('commentForm').addEventListener('submit', async function(e) {{
                        e.preventDefault();
                        const formData = new FormData(this);
                        const comment = formData.get('comment');
                        
                        const response = await fetch(`/recipes/{recipe_id}/comments`, {{
                            method: 'POST',
                            headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify({{ comment: comment }})
                        }});
                        
                        if (response.ok) {{
                            alert('Comment added successfully!');
                            location.reload();
                        }} else {{
                            alert('Error adding comment');
                        }}
                    }});
                    
                    document.getElementById('ratingForm').addEventListener('submit', async function(e) {{
                        e.preventDefault();
                        const formData = new FormData(this);
                        const rating = formData.get('rating');
                        
                        const response = await fetch(`/recipes/{recipe_id}/ratings`, {{
                            method: 'POST',
                            headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify({{ rating: parseInt(rating) }})
                        }});
                        
                        if (response.ok) {{
                            alert('Rating added successfully!');
                            location.reload();
                        }} else {{
                            alert('Error adding rating');
                        }}
                    }});
                </script>
            </body>
            </html>
            """
            
            return html, 200, {'Content-Type': 'text/html'}
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        release_db_connection(conn)

@app.route('/recipes/<int:recipe_id>/comments', methods=['POST'])
def add_comment(recipe_id):
    """Add a comment to a recipe."""
    data = request.get_json()
    
    if not data:
        return jsonify({'error': 'No JSON data provided'}), 400
    
    if 'comment' not in data:
        return jsonify({'error': 'Missing required field: comment'}), 400
    
    comment = data['comment']
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Check if recipe exists
            cur.execute("SELECT id FROM recipes WHERE id = %s", (recipe_id,))
            if not cur.fetchone():
                abort(404, description="Recipe not found")
            
            # Add comment
            cur.execute("""
                INSERT INTO comments (recipe_id, comment)
                VALUES (%s, %s)
            """, (recipe_id, comment))
            
            conn.commit()
            return jsonify({'message': 'Comment added successfully'}), 201
            
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        release_db_connection(conn)

@app.route('/recipes/<int:recipe_id>/ratings', methods=['POST'])
def add_rating(recipe_id):
    """Rate a recipe."""
    data = request.get_json()
    
    if not data:
        return jsonify({'error': 'No JSON data provided'}), 400
    
    if 'rating' not in data:
        return jsonify({'error': 'Missing required field: rating'}), 400
    
    rating = data['rating']
    
    # Validate rating is between 1 and 5
    if not isinstance(rating, int) or rating < 1 or rating > 5:
        return jsonify({'error': 'Rating must be an integer between 1 and 5'}), 400
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Check if recipe exists
            cur.execute("SELECT id FROM recipes WHERE id = %s", (recipe_id,))
            if not cur.fetchone():
                abort(404, description="Recipe not found")
            
            # Add rating
            cur.execute("""
                INSERT INTO ratings (recipe_id, rating)
                VALUES (%s, %s)
            """, (recipe_id, rating))
            
            conn.commit()
            return jsonify({'message': 'Rating added successfully'}), 201
            
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        release_db_connection(conn)

@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors."""
    return jsonify({'error': str(error.description)}), 404

@app.errorhandler(400)
def bad_request(error):
    """Handle 400 errors."""
    return jsonify({'error': str(error.description)}), 400

if __name__ == '__main__':
    # Create app
    app = create_app()
    
    # Get port from environment variable or use default
    port = int(os.environ.get('PORT', 5001))
    
    # Run the app
    app.run(host='0.0.0.0', port=port)