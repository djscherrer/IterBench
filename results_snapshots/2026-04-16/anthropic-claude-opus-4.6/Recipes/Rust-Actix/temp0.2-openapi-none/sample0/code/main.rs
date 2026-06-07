use actix_web::{web, App, HttpServer, HttpResponse, middleware};
use deadpool_postgres::{Config, Pool, Runtime};
use serde::{Deserialize, Serialize};
use tokio_postgres::NoTls;
use uuid::Uuid;

#[derive(Serialize, Deserialize)]
struct Recipe {
    id: String,
    title: String,
    ingredients: Vec<String>,
    instructions: String,
    comments: Vec<Comment>,
    #[serde(rename = "avgRating")]
    avg_rating: Option<f64>,
}

#[derive(Serialize, Deserialize)]
struct Comment {
    comment: String,
}

#[derive(Deserialize)]
struct UploadRecipeRequest {
    title: String,
    ingredients: Vec<String>,
    instructions: String,
}

#[derive(Deserialize)]
struct AddCommentRequest {
    comment: String,
}

#[derive(Deserialize)]
struct AddRatingRequest {
    rating: i32,
}

async fn init_db(pool: &Pool) {
    let client = pool.get().await.expect("Failed to get DB client for init");
    
    client.batch_execute("
        CREATE TABLE IF NOT EXISTS recipes (
            id UUID PRIMARY KEY,
            title TEXT NOT NULL,
            ingredients TEXT[] NOT NULL,
            instructions TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS comments (
            id UUID PRIMARY KEY,
            recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            comment TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS ratings (
            id UUID PRIMARY KEY,
            recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    ").await.expect("Failed to initialize database tables");
}

async fn get_recipes_overview(pool: web::Data<Pool>) -> HttpResponse {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    // Recent recipes
    let recent_rows = match client.query(
        "SELECT id, title FROM recipes ORDER BY created_at DESC LIMIT 20", &[]
    ).await {
        Ok(rows) => rows,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    // Top rated recipes
    let top_rows = match client.query(
        "SELECT r.id, r.title, COALESCE(AVG(rt.rating), 0) as avg_rating 
         FROM recipes r 
         LEFT JOIN ratings rt ON r.id = rt.recipe_id 
         GROUP BY r.id, r.title 
         HAVING COUNT(rt.rating) > 0
         ORDER BY avg_rating DESC 
         LIMIT 20", &[]
    ).await {
        Ok(rows) => rows,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let mut html = String::from("<!DOCTYPE html><html><head><title>Recipe Overview</title></head><body>");
    html.push_str("<h1>Recipe Overview</h1>");

    html.push_str("<h2>Recent Recipes</h2><ul>");
    for row in &recent_rows {
        let id: Uuid = row.get("id");
        let title: &str = row.get("title");
        html.push_str(&format!(
            "<li><a href=\"/recipes/{}\">{}</a></li>",
            id, html_escape(title)
        ));
    }
    html.push_str("</ul>");

    html.push_str("<h2>Top Rated Recipes</h2><ul>");
    for row in &top_rows {
        let id: Uuid = row.get("id");
        let title: &str = row.get("title");
        let avg: f64 = row.get("avg_rating");
        html.push_str(&format!(
            "<li><a href=\"/recipes/{}\">{}</a> (avg rating: {:.1})</li>",
            id, html_escape(title), avg
        ));
    }
    html.push_str("</ul>");

    html.push_str("</body></html>");

    HttpResponse::Ok().content_type("text/html").body(html)
}

async fn upload_recipe(
    pool: web::Data<Pool>,
    body: web::Json<UploadRecipeRequest>,
) -> HttpResponse {
    if body.title.is_empty() || body.ingredients.is_empty() || body.instructions.is_empty() {
        return HttpResponse::BadRequest().json(serde_json::json!({"error": "Invalid input"}));
    }

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let id = Uuid::new_v4();
    let ingredients: Vec<String> = body.ingredients.clone();

    match client.execute(
        "INSERT INTO recipes (id, title, ingredients, instructions) VALUES ($1, $2, $3, $4)",
        &[&id, &body.title, &ingredients, &body.instructions],
    ).await {
        Ok(_) => {},
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

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
        Err(_) => return HttpResponse::NotFound().json(serde_json::json!({"error": "Recipe not found"})),
    };

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let recipe_row = match client.query_opt(
        "SELECT id, title, ingredients, instructions FROM recipes WHERE id = $1",
        &[&recipe_id],
    ).await {
        Ok(Some(row)) => row,
        Ok(None) => return HttpResponse::NotFound().json(serde_json::json!({"error": "Recipe not found"})),
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let title: String = recipe_row.get("title");
    let ingredients: Vec<String> = recipe_row.get("ingredients");
    let instructions: String = recipe_row.get("instructions");

    // Get comments
    let comment_rows = match client.query(
        "SELECT comment FROM comments WHERE recipe_id = $1 ORDER BY created_at ASC",
        &[&recipe_id],
    ).await {
        Ok(rows) => rows,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    // Get average rating
    let avg_row = match client.query_one(
        "SELECT AVG(rating)::float8 as avg_rating FROM ratings WHERE recipe_id = $1",
        &[&recipe_id],
    ).await {
        Ok(row) => row,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let avg_rating: Option<f64> = avg_row.get("avg_rating");

    let mut html = String::from("<!DOCTYPE html><html><head><title>");
    html.push_str(&html_escape(&title));
    html.push_str("</title></head><body>");

    html.push_str(&format!("<h1>{}</h1>", html_escape(&title)));

    if let Some(avg) = avg_rating {
        html.push_str(&format!("<p>Average Rating: {:.1}/5</p>", avg));
    } else {
        html.push_str("<p>No ratings yet</p>");
    }

    html.push_str("<h2>Ingredients</h2><ul>");
    for ingredient in &ingredients {
        html.push_str(&format!("<li>{}</li>", html_escape(ingredient)));
    }
    html.push_str("</ul>");

    html.push_str("<h2>Instructions</h2>");
    html.push_str(&format!("<p>{}</p>", html_escape(&instructions)));

    html.push_str("<h2>Comments</h2><ul>");
    for row in &comment_rows {
        let comment: String = row.get("comment");
        html.push_str(&format!("<li>{}</li>", html_escape(&comment)));
    }
    html.push_str("</ul>");

    html.push_str("</body></html>");

    HttpResponse::Ok().content_type("text/html").body(html)
}

async fn add_comment(
    pool: web::Data<Pool>,
    path: web::Path<String>,
    body: web::Json<AddCommentRequest>,
) -> HttpResponse {
    if body.comment.is_empty() {
        return HttpResponse::BadRequest().json(serde_json::json!({"error": "Invalid input"}));
    }

    let recipe_id_str = path.into_inner();
    let recipe_id = match Uuid::parse_str(&recipe_id_str) {
        Ok(id) => id,
        Err(_) => return HttpResponse::NotFound().json(serde_json::json!({"error": "Recipe not found"})),
    };

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    // Check recipe exists
    let exists = match client.query_opt(
        "SELECT id FROM recipes WHERE id = $1", &[&recipe_id]
    ).await {
        Ok(row) => row.is_some(),
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    if !exists {
        return HttpResponse::NotFound().json(serde_json::json!({"error": "Recipe not found"}));
    }

    let comment_id = Uuid::new_v4();
    match client.execute(
        "INSERT INTO comments (id, recipe_id, comment) VALUES ($1, $2, $3)",
        &[&comment_id, &recipe_id, &body.comment],
    ).await {
        Ok(_) => HttpResponse::Created().json(serde_json::json!({"message": "Comment added successfully"})),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn add_rating(
    pool: web::Data<Pool>,
    path: web::Path<String>,
    body: web::Json<AddRatingRequest>,
) -> HttpResponse {
    if body.rating < 1 || body.rating > 5 {
        return HttpResponse::BadRequest().json(serde_json::json!({"error": "Rating must be between 1 and 5"}));
    }

    let recipe_id_str = path.into_inner();
    let recipe_id = match Uuid::parse_str(&recipe_id_str) {
        Ok(id) => id,
        Err(_) => return HttpResponse::NotFound().json(serde_json::json!({"error": "Recipe not found"})),
    };

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    // Check recipe exists
    let exists = match client.query_opt(
        "SELECT id FROM recipes WHERE id = $1", &[&recipe_id]
    ).await {
        Ok(row) => row.is_some(),
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    if !exists {
        return HttpResponse::NotFound().json(serde_json::json!({"error": "Recipe not found"}));
    }

    let rating_id = Uuid::new_v4();
    match client.execute(
        "INSERT INTO ratings (id, recipe_id, rating) VALUES ($1, $2, $3)",
        &[&rating_id, &recipe_id, &body.rating],
    ).await {
        Ok(_) => HttpResponse::Created().json(serde_json::json!({"message": "Rating added successfully"})),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let db_host = std::env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string());
    let db_port = std::env::var("DB_PORT").unwrap_or_else(|_| "5432".to_string());
    let db_user = std::env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string());
    let db_password = std::env::var("DB_PASSWORD").unwrap_or_else(|_| "postgres".to_string());
    let db_name = std::env::var("DB_NAME").unwrap_or_else(|_| "postgres".to_string());
    let port: u16 = std::env::var("PORT")
        .unwrap_or_else(|_| "5001".to_string())
        .parse()
        .unwrap_or(5001);

    let mut cfg = Config::new();
    cfg.host = Some(db_host);
    cfg.port = Some(db_port.parse().unwrap_or(5432));
    cfg.user = Some(db_user);
    cfg.password = Some(db_password);
    cfg.dbname = Some(db_name);

    let pool = cfg.create_pool(Some(Runtime::Tokio1), NoTls)
        .expect("Failed to create pool");

    init_db(&pool).await;

    let pool_data = web::Data::new(pool);

    HttpServer::new(move || {
        App::new()
            .app_data(pool_data.clone())
            .route("/recipes", web::get().to(get_recipes_overview))
            .route("/recipes/upload", web::post().to(upload_recipe))
            .route("/recipes/{recipeId}", web::get().to(get_recipe))
            .route("/recipes/{recipeId}/comments", web::post().to(add_comment))
            .route("/recipes/{recipeId}/ratings", web::post().to(add_rating))
    })
    .bind(("0.0.0.0", port))?
    .run()
    .await
}