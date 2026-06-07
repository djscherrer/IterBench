import os
import uuid
from flask import Flask, request, jsonify, Response
import psycopg2
import psycopg2.extras

app = Flask(__name__)

DB_HOST = os.environ.get('DB_HOST', 'localhost')
DB_PORT = os.environ.get('DB_PORT', '5432')
DB_USER = os.environ.get('DB_USER', 'postgres')
DB_PASSWORD = os.environ.get('DB_PASSWORD', 'postgres')
DB_NAME = os.environ.get('DB_NAME', 'recipes')


def get_db_connection():
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname=DB_NAME
    )
    return conn


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS recipes (
            id VARCHAR(64) PRIMARY KEY,
            title VARCHAR(512) NOT NULL,
            ingredients TEXT NOT NULL,
            instructions TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id SERIAL PRIMARY KEY,
            recipe_id VARCHAR(64) NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            comment TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ratings (
            id SERIAL PRIMARY KEY,
            recipe_id VARCHAR(64) NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    conn.commit()
    cur.close()
    conn.close()


init_db()


@app.route('/recipes', methods=['GET'])
def get_recipes_overview():
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Get recent recipes
        cur.execute("""
            SELECT r.id, r.title, COALESCE(AVG(rt.rating), NULL) as avg_rating
            FROM recipes r
            LEFT JOIN ratings rt ON r.id = rt.recipe_id
            GROUP BY r.id, r.title, r.created_at
            ORDER BY r.created_at DESC
            LIMIT 10
        """)
        recent_recipes = cur.fetchall()

        # Get top-rated recipes
        cur.execute("""
            SELECT r.id, r.title, AVG(rt.rating) as avg_rating
            FROM recipes r
            INNER JOIN ratings rt ON r.id = rt.recipe_id
            GROUP BY r.id, r.title
            ORDER BY avg_rating DESC
            LIMIT 10
        """)
        top_rated_recipes = cur.fetchall()

        cur.close()
        conn.close()

        html = """<!DOCTYPE html>
<html>
<head><title>Recipe Overview</title></head>
<body>
<h1>Recipe Overview</h1>
<h2>Recent Recipes</h2>
<ul>
"""
        for r in recent_recipes:
            avg = f" (Avg Rating: {float(r['avg_rating']):.1f})" if r['avg_rating'] is not None else ""
            html += f'<li><a href="/recipes/{r["id"]}">{r["title"]}</a>{avg}</li>\n'

        html += """</ul>
<h2>Top Rated Recipes</h2>
<ul>
"""
        for r in top_rated_recipes:
            avg = f" (Avg Rating: {float(r['avg_rating']):.1f})" if r['avg_rating'] is not None else ""
            html += f'<li><a href="/recipes/{r["id"]}">{r["title"]}</a>{avg}</li>\n'

        html += """</ul>
</body>
</html>"""

        return Response(html, status=200, content_type='text/html')
    except Exception as e:
        return Response(f"Server error: {str(e)}", status=500)


@app.route('/recipes/upload', methods=['POST'])
def upload_recipe():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid input"}), 400

    title = data.get('title')
    ingredients = data.get('ingredients')
    instructions = data.get('instructions')

    if not title or not ingredients or not instructions:
        return jsonify({"error": "Missing required fields: title, ingredients, instructions"}), 400

    if not isinstance(ingredients, list):
        return jsonify({"error": "Ingredients must be an array"}), 400

    recipe_id = str(uuid.uuid4())
    ingredients_str = '|||'.join(ingredients)

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO recipes (id, title, ingredients, instructions) VALUES (%s, %s, %s, %s)",
        (recipe_id, title, ingredients_str, instructions)
    )
    conn.commit()
    cur.close()
    conn.close()

    recipe = {
        "id": recipe_id,
        "title": title,
        "ingredients": ingredients,
        "instructions": instructions,
        "comments": [],
        "avgRating": None
    }

    return jsonify(recipe), 201


@app.route('/recipes/<recipeId>', methods=['GET'])
def get_recipe(recipeId):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM recipes WHERE id = %s", (recipeId,))
    recipe = cur.fetchone()

    if not recipe:
        cur.close()
        conn.close()
        return Response("Recipe not found", status=404)

    cur.execute("SELECT comment FROM comments WHERE recipe_id = %s ORDER BY created_at", (recipeId,))
    comments = cur.fetchall()

    cur.execute("SELECT AVG(rating) as avg_rating FROM ratings WHERE recipe_id = %s", (recipeId,))
    rating_row = cur.fetchone()
    avg_rating = float(rating_row['avg_rating']) if rating_row and rating_row['avg_rating'] is not None else None

    cur.close()
    conn.close()

    ingredients = recipe['ingredients'].split('|||')

    html = f"""<!DOCTYPE html>
<html>
<head><title>{recipe['title']}</title></head>
<body>
<h1>{recipe['title']}</h1>
<h2>Ingredients</h2>
<ul>
"""
    for ing in ingredients:
        html += f"<li>{ing}</li>\n"

    html += f"""</ul>
<h2>Instructions</h2>
<p>{recipe['instructions']}</p>
<h2>Average Rating</h2>
<p>{f"{avg_rating:.1f} / 5" if avg_rating is not None else "No ratings yet"}</p>
<h2>Comments</h2>
<ul>
"""
    for c in comments:
        html += f"<li>{c['comment']}</li>\n"

    if not comments:
        html += "<li>No comments yet</li>\n"

    html += """</ul>
</body>
</html>"""

    return Response(html, status=200, content_type='text/html')


@app.route('/recipes/<recipeId>/comments', methods=['POST'])
def add_comment(recipeId):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT id FROM recipes WHERE id = %s", (recipeId,))
    if not cur.fetchone():
        cur.close()
        conn.close()
        return jsonify({"error": "Recipe not found"}), 404

    data = request.get_json()
    if not data or 'comment' not in data or not data['comment']:
        cur.close()
        conn.close()
        return jsonify({"error": "Invalid input, 'comment' is required"}), 400

    comment = data['comment']

    cur.execute(
        "INSERT INTO comments (recipe_id, comment) VALUES (%s, %s)",
        (recipeId, comment)
    )
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"message": "Comment added successfully"}), 201


@app.route('/recipes/<recipeId>/ratings', methods=['POST'])
def add_rating(recipeId):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT id FROM recipes WHERE id = %s", (recipeId,))
    if not cur.fetchone():
        cur.close()
        conn.close()
        return jsonify({"error": "Recipe not found"}), 404

    data = request.get_json()
    if not data or 'rating' not in data:
        cur.close()
        conn.close()
        return jsonify({"error": "Invalid input, 'rating' is required"}), 400

    rating = data['rating']

    if not isinstance(rating, int) or rating < 1 or rating > 5:
        cur.close()
        conn.close()
        return jsonify({"error": "Rating must be an integer between 1 and 5"}), 400

    cur.execute(
        "INSERT INTO ratings (recipe_id, rating) VALUES (%s, %s)",
        (recipeId, rating)
    )
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"message": "Rating added successfully"}), 201


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)