use actix_web::{web, App, HttpServer, HttpResponse};
use deadpool_postgres::{Config, Pool, Runtime};
use serde::{Deserialize, Serialize};
use std::env;
use tokio_postgres::NoTls;
use uuid::Uuid;

// ---- Data structures ----

#[derive(Debug, Serialize, Deserialize)]
struct RecipeUpload {
    title: String,
    ingredients: Vec<String>,
    instructions: String,
}

#[derive(Debug, Serialize)]
struct Recipe {
    id: String,
    title: String,
    ingredients: Vec<String>,
    instructions: String,
    comments: Vec<Comment>,
    #[serde(rename = "avgRating")]
    avg_rating: Option<f64>,
}

#[derive(Debug, Serialize)]
struct Comment {
    comment: String,
}

#[derive(Debug, Deserialize)]
struct CommentUpload {
    comment: String,
}

#[derive(Debug, Deserialize)]
struct RatingUpload {
    rating: i32,
}

// ---- Database initialization ----

async fn init_db(pool: &Pool) -> Result<(), Box<dyn std::error::Error>> {
    let client = pool.get().await?;

    client
        .batch_execute(
            "
        CREATE TABLE IF NOT EXISTS recipes (
            id UUID PRIMARY KEY,
            title TEXT NOT NULL,
            ingredients TEXT NOT NULL,
            instructions TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS comments (
            id UUID PRIMARY KEY,
            recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            comment TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS ratings (
            id UUID PRIMARY KEY,
            recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_comments_recipe_id ON comments(recipe_id);
        CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id ON ratings(recipe_id);
        CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes(created_at DESC);
        ",
        )
        .await?;

    Ok(())
}

// ---- Handlers ----

async fn get_recipes(pool: web::Data<Pool>) -> HttpResponse {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Failed to get db connection: {}", e);
            return HttpResponse::InternalServerError().body("Server error");
        }
    };

    // Get recent recipes (last 20)
    let recent_rows = match client
        .query(
            "SELECT id, title FROM recipes ORDER BY created_at DESC LIMIT 20",
            &[],
        )
        .await
    {
        Ok(rows) => rows,
        Err(e) => {
            eprintln!("Database query error: {}", e);
            return HttpResponse::InternalServerError().body("Server error");
        }
    };

    // Get top rated recipes (last 20 with avg rating)
    let top_rows = match client
        .query(
            "SELECT r.id, r.title, COALESCE(AVG(rt.rating), 0) as avg_rating \
             FROM recipes r \
             LEFT JOIN ratings rt ON r.id = rt.recipe_id \
             GROUP BY r.id, r.title \
             ORDER BY avg_rating DESC, r.created_at DESC \
             LIMIT 20",
            &[],
        )
        .await
    {
        Ok(rows) => rows,
        Err(e) => {
            eprintln!("Database query error: {}", e);
            return HttpResponse::InternalServerError().body("Server error");
        }
    };

    let mut html = String::from(
        "<!DOCTYPE html><html><head><title>Recipe Sharing</title>\
         <meta charset=\"utf-8\"><style>\
         body{font-family:sans-serif;max-width:800px;margin:0 auto;padding:20px;}\
         h1{color:#333}ul{list-style:none;padding:0}\
         li{margin:8px 0}a{color:#0066cc;text-decoration:none}\
         .section{margin-bottom:30px}\
         </style></head><body>\
         <h1>Recipe Sharing App</h1>",
    );

    html.push_str("<div class=\"section\"><h2>Recent Recipes</h2><ul>");
    for row in &recent_rows {
        let id: Uuid = row.get(0);
        let title: &str = row.get(1);
        html.push_str(&format!(
            "<li><a href=\"/recipes/{}\">{}</a></li>",
            id, title
        ));
    }
    if recent_rows.is_empty() {
        html.push_str("<li>No recipes yet.</li>");
    }
    html.push_str("</ul></div>");

    html.push_str("<div class=\"section\"><h2>Top Rated Recipes</h2><ul>");
    for row in &top_rows {
        let id: Uuid = row.get(0);
        let title: &str = row.get(1);
        let avg: f64 = row.get(2);
        html.push_str(&format!(
            "<li><a href=\"/recipes/{}\">{}</a> (Avg: {:.1})</li>",
            id, title, avg
        ));
    }
    if top_rows.is_empty() {
        html.push_str("<li>No recipes yet.</li>");
    }
    html.push_str("</ul></div>");

    html.push_str("</body></html>");

    HttpResponse::Ok()
        .content_type("text/html; charset=utf-8")
        .body(html)
}

async fn upload_recipe(
    pool: web::Data<Pool>,
    body: web::Json<RecipeUpload>,
) -> HttpResponse {
    if body.title.trim().is_empty() || body.instructions.trim().is_empty() || body.ingredients.is_empty() {
        return HttpResponse::BadRequest().json(serde_json::json!({
            "error": "Title, ingredients, and instructions are required"
        }));
    }

    let id = Uuid::new_v4();

    let client = match pool.get().await {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Failed to get db connection: {}", e);
            return HttpResponse::InternalServerError().body("Server error");
        }
    };

    let ingredients_json = serde_json::to_string(&body.ingredients).unwrap();

    match client
        .execute(
            "INSERT INTO recipes (id, title, ingredients, instructions) VALUES ($1, $2, $3, $4)",
            &[&id, &body.title, &ingredients_json, &body.instructions],
        )
        .await
    {
        Ok(_) => {}
        Err(e) => {
            eprintln!("Failed to insert recipe: {}", e);
            return HttpResponse::InternalServerError().body("Server error");
        }
    }

    let recipe = Recipe {
        id: id.to_string(),
        title: body.title.clone(),
        ingredients: body.ingredients.clone(),
        instructions: body.instructions.clone(),
        comments: vec![],
        avg_rating: None,
    };

    HttpResponse::Created().json(recipe)
}

async fn get_recipe(
    pool: web::Data<Pool>,
    path: web::Path<String>,
) -> HttpResponse {
    let recipe_id_str = path.into_inner();
    let recipe_id = match Uuid::parse_str(&recipe_id_str) {
        Ok(id) => id,
        Err(_) => return HttpResponse::NotFound().body("Recipe not found"),
    };

    let client = match pool.get().await {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Failed to get db connection: {}", e);
            return HttpResponse::InternalServerError().body("Server error");
        }
    };

    // Get recipe
    let recipe_row = match client
        .query_opt(
            "SELECT id, title, ingredients, instructions FROM recipes WHERE id = $1",
            &[&recipe_id],
        )
        .await
    {
        Ok(row) => row,
        Err(e) => {
            eprintln!("Database query error: {}", e);
            return HttpResponse::InternalServerError().body("Server error");
        }
    };

    let recipe_row = match recipe_row {
        Some(r) => r,
        None => return HttpResponse::NotFound().body("Recipe not found"),
    };

    let id: Uuid = recipe_row.get(0);
    let title: &str = recipe_row.get(1);
    let ingredients_json: &str = recipe_row.get(2);
    let instructions: &str = recipe_row.get(3);

    let ingredients_list: Vec<String> =
        serde_json::from_str(ingredients_json).unwrap_or_default();

    // Get comments
    let comments_rows = match client
        .query(
            "SELECT comment FROM comments WHERE recipe_id = $1 ORDER BY created_at DESC",
            &[&recipe_id],
        )
        .await
    {
        Ok(rows) => rows,
        Err(e) => {
            eprintln!("Database query error: {}", e);
            return HttpResponse::InternalServerError().body("Server error");
        }
    };

    // Get average rating
    let avg_rating: Option<f64> = match client
        .query_one(
            "SELECT AVG(rating)::float FROM ratings WHERE recipe_id = $1",
            &[&recipe_id],
        )
        .await
    {
        Ok(row) => row.get(0),
        Err(e) => {
            eprintln!("Database query error: {}", e);
            return HttpResponse::InternalServerError().body("Server error");
        }
    };

    let mut html = String::from(
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Recipe</title>\
         <style>body{font-family:sans-serif;max-width:800px;margin:0 auto;padding:20px;}\
         h1{color:#333}ul{list-style:none;padding:0}li{margin:4px 0}\
         .comments{margin-top:20px}.comment{background:#f5f5f5;padding:10px;margin:8px 0;border-radius:4px}\
         .rating{font-size:1.2em;color:#e67e22}\
         .back-link{display:inline-block;margin-bottom:20px;color:#0066cc}\
         form{margin-top:20px;padding:15px;background:#f9f9f9;border-radius:4px}\
         input,textarea{width:100%;padding:8px;margin:4px 0;box-sizing:border-box}\
         button{padding:10px 20px;background:#0066cc;color:white;border:none;border-radius:4px;cursor:pointer}\
         button:hover{background:#004c99}\
         </style></head><body>",
    );

    html.push_str(&format!(
        "<a class=\"back-link\" href=\"/recipes\">&larr; Back to Recipes</a>\
         <h1>{}</h1>",
        title
    ));

    if let Some(avg) = avg_rating {
        html.push_str(&format!("<p class=\"rating\">Average Rating: {:.1} / 5</p>", avg));
    } else {
        html.push_str("<p class=\"rating\">No ratings yet</p>");
    }

    html.push_str("<h3>Ingredients</h3><ul>");
    for ing in &ingredients_list {
        html.push_str(&format!("<li>{}</li>", ing));
    }
    html.push_str("</ul>");

    html.push_str(&format!("<h3>Instructions</h3><p>{}</p>", instructions));

    // Comments section
    html.push_str("<div class=\"comments\"><h3>Comments</h3>");
    if comments_rows.is_empty() {
        html.push_str("<p>No comments yet.</p>");
    } else {
        for row in &comments_rows {
            let comment: &str = row.get(0);
            html.push_str(&format!("<div class=\"comment\">{}</div>", comment));
        }
    }
    html.push_str("</div>");

    // Add comment form
    html.push_str(&format!(
        "<form onsubmit=\"event.preventDefault();addComment('{}');return false;\">\
         <h4>Add a Comment</h4>\
         <input type=\"text\" id=\"comment-text\" placeholder=\"Write a comment...\" required>\
         <button type=\"submit\">Submit Comment</button>\
         </form>",
        id
    ));

    // Add rating form
    html.push_str(&format!(
        "<form onsubmit=\"event.preventDefault();addRating('{}');return false;\">\
         <h4>Rate this Recipe</h4>\
         <select id=\"rating-value\" required>\
         <option value=\"\">Select rating...</option>\
         <option value=\"1\">1</option><option value=\"2\">2</option><option value=\"3\">3</option>\
         <option value=\"4\">4</option><option value=\"5\">5</option>\
         </select>\
         <button type=\"submit\">Submit Rating</button>\
         </form>\
         <div id=\"rating-result\"></div>",
        id
    ));

    // JavaScript for forms
    html.push_str(
        "<script>
        async function addComment(recipeId) {
            const text = document.getElementById('comment-text').value;
            const resp = await fetch('/recipes/' + recipeId + '/comments', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({comment: text})
            });
            if (resp.ok) { location.reload(); }
            else { alert('Error adding comment'); }
        }
        async function addRating(recipeId) {
            const val = document.getElementById('rating-value').value;
            if (!val) return;
            const resp = await fetch('/recipes/' + recipeId + '/ratings', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({rating: parseInt(val)})
            });
            if (resp.ok) { location.reload(); }
            else { 
                const err = await resp.text();
                document.getElementById('rating-result').textContent = 'Error: ' + err;
            }
        }
        </script>",
    );

    html.push_str("</body></html>");

    HttpResponse::Ok()
        .content_type("text/html; charset=utf-8")
        .body(html)
}

async fn add_comment(
    pool: web::Data<Pool>,
    path: web::Path<String>,
    body: web::Json<CommentUpload>,
) -> HttpResponse {
    let recipe_id_str = path.into_inner();
    let recipe_id = match Uuid::parse_str(&recipe_id_str) {
        Ok(id) => id,
        Err(_) => return HttpResponse::NotFound().body("Recipe not found"),
    };

    if body.comment.trim().is_empty() {
        return HttpResponse::BadRequest().body("Comment cannot be empty");
    }

    let client = match pool.get().await {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Failed to get db connection: {}", e);
            return HttpResponse::InternalServerError().body("Server error");
        }
    };

    // Check recipe exists
    let exists = match client
        .query_opt("SELECT 1 FROM recipes WHERE id = $1", &[&recipe_id])
        .await
    {
        Ok(row) => row.is_some(),
        Err(e) => {
            eprintln!("Database query error: {}", e);
            return HttpResponse::InternalServerError().body("Server error");
        }
    };

    if !exists {
        return HttpResponse::NotFound().body("Recipe not found");
    }

    let comment_id = Uuid::new_v4();
    match client
        .execute(
            "INSERT INTO comments (id, recipe_id, comment) VALUES ($1, $2, $3)",
            &[&comment_id, &recipe_id, &body.comment],
        )
        .await
    {
        Ok(_) => HttpResponse::Created()
            .json(serde_json::json!({"status": "Comment added successfully"})),
        Err(e) => {
            eprintln!("Failed to insert comment: {}", e);
            HttpResponse::InternalServerError().body("Server error")
        }
    }
}

async fn add_rating(
    pool: web::Data<Pool>,
    path: web::Path<String>,
    body: web::Json<RatingUpload>,
) -> HttpResponse {
    let recipe_id_str = path.into_inner();
    let recipe_id = match Uuid::parse_str(&recipe_id_str) {
        Ok(id) => id,
        Err(_) => return HttpResponse::NotFound().body("Recipe not found"),
    };

    if body.rating < 1 || body.rating > 5 {
        return HttpResponse::BadRequest().body("Rating must be between 1 and 5");
    }

    let client = match pool.get().await {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Failed to get db connection: {}", e);
            return HttpResponse::InternalServerError().body("Server error");
        }
    };

    // Check recipe exists
    let exists = match client
        .query_opt("SELECT 1 FROM recipes WHERE id = $1", &[&recipe_id])
        .await
    {
        Ok(row) => row.is_some(),
        Err(e) => {
            eprintln!("Database query error: {}", e);
            return HttpResponse::InternalServerError().body("Server error");
        }
    };

    if !exists {
        return HttpResponse::NotFound().body("Recipe not found");
    }

    let rating_id = Uuid::new_v4();
    match client
        .execute(
            "INSERT INTO ratings (id, recipe_id, rating) VALUES ($1, $2, $3)",
            &[&rating_id, &recipe_id, &body.rating],
        )
        .await
    {
        Ok(_) => HttpResponse::Created()
            .json(serde_json::json!({"status": "Rating added successfully"})),
        Err(e) => {
            eprintln!("Failed to insert rating: {}", e);
            HttpResponse::InternalServerError().body("Server error")
        }
    }
}

// ---- Main ----

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init_from_env(env_logger::Env::default().default_filter_or("info"));

    let host = env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string());
    let port: u16 = env::var("DB_PORT")
        .unwrap_or_else(|_| "5432".to_string())
        .parse()
        .unwrap_or(5432);
    let user = env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string());
    let password = env::var("DB_PASSWORD").unwrap_or_else(|_| "postgres".to_string());
    let dbname = env::var("DB_NAME").unwrap_or_else(|_| "testdb".to_string());

    let mut cfg = Config::new();
    cfg.host = Some(host);
    cfg.port = Some(port);
    cfg.user = Some(user);
    cfg.password = Some(password);
    cfg.dbname = Some(dbname);

    let pool = cfg
        .create_pool(Some(Runtime::Tokio1), NoTls)
        .expect("Failed to create database pool");

    // Initialize database tables
    init_db(&pool)
        .await
        .expect("Failed to initialize database");

    let server_port: u16 = env::var("PORT")
        .unwrap_or_else(|_| "5001".to_string())
        .parse()
        .unwrap_or(5001);

    eprintln!("Starting server on 0.0.0.0:{}", server_port);

    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(pool.clone()))
            .route("/recipes", web::get().to(get_recipes))
            .route("/recipes/upload", web::post().to(upload_recipe))
            .route("/recipes/{recipeId}", web::get().to(get_recipe))
            .route(
                "/recipes/{recipeId}/comments",
                web::post().to(add_comment),
            )
            .route(
                "/recipes/{recipeId}/ratings",
                web::post().to(add_rating),
            )
    })
    .bind(format!("0.0.0.0:{}", server_port))?
    .run()
    .await
}
