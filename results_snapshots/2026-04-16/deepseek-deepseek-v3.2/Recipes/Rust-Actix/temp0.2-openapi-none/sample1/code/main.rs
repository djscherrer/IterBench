use actix_web::{web, App, HttpResponse, HttpServer, Responder, Result, get, post};
use deadpool_postgres::{Config, ManagerConfig, Pool, RecyclingMethod, Runtime};
use serde::{Deserialize, Serialize};
use std::env;
use uuid::Uuid;

#[derive(Deserialize)]
struct RecipeUpload {
    title: String,
    ingredients: Vec<String>,
    instructions: String,
}

#[derive(Deserialize)]
struct CommentRequest {
    comment: String,
}

#[derive(Deserialize)]
struct RatingRequest {
    rating: i32,
}

#[derive(Serialize)]
struct Recipe {
    id: String,
    title: String,
    ingredients: Vec<String>,
    instructions: String,
    comments: Vec<Comment>,
    avg_rating: Option<f64>,
}

#[derive(Serialize)]
struct Comment {
    comment: String,
}

async fn get_recipes_overview(pool: web::Data<Pool>) -> Result<impl Responder> {
    let client = pool.get().await.map_err(|e| {
        actix_web::error::ErrorInternalServerError(format!("Database connection error: {}", e))
    })?;

    let rows = client
        .query(
            "SELECT id, title FROM recipes ORDER BY created_at DESC LIMIT 10",
            &[],
        )
        .await
        .map_err(|e| {
            actix_web::error::ErrorInternalServerError(format!("Query error: {}", e))
        })?;

    let mut html = String::from("<html><head><title>Recipe Overview</title></head><body><h1>Recent Recipes</h1><ul>");
    for row in rows {
        let id: Uuid = row.get(0);
        let title: String = row.get(1);
        html.push_str(&format!(
            "<li><a href=\"/recipes/{}\">{}</a></li>",
            id, title
        ));
    }
    html.push_str("</ul></body></html>");

    Ok(HttpResponse::Ok().content_type("text/html").body(html))
}

async fn upload_recipe(
    pool: web::Data<Pool>,
    recipe: web::Json<RecipeUpload>,
) -> Result<impl Responder> {
    let client = pool.get().await.map_err(|e| {
        actix_web::error::ErrorInternalServerError(format!("Database connection error: {}", e))
    })?;

    let id = Uuid::new_v4();
    let ingredients_json = serde_json::to_value(&recipe.ingredients).map_err(|e| {
        actix_web::error::ErrorBadRequest(format!("Invalid ingredients format: {}", e))
    })?;

    client
        .execute(
            "INSERT INTO recipes (id, title, ingredients, instructions) VALUES ($1, $2, $3, $4)",
            &[&id, &recipe.title, &ingredients_json, &recipe.instructions],
        )
        .await
        .map_err(|e| {
            actix_web::error::ErrorInternalServerError(format!("Insert error: {}", e))
        })?;

    let response = Recipe {
        id: id.to_string(),
        title: recipe.title.clone(),
        ingredients: recipe.ingredients.clone(),
        instructions: recipe.instructions.clone(),
        comments: Vec::new(),
        avg_rating: None,
    };

    Ok(HttpResponse::Created().json(response))
}

async fn get_recipe(pool: web::Data<Pool>, recipe_id: web::Path<String>) -> Result<impl Responder> {
    let client = pool.get().await.map_err(|e| {
        actix_web::error::ErrorInternalServerError(format!("Database connection error: {}", e))
    })?;

    let uuid = Uuid::parse_str(&recipe_id).map_err(|_| {
        actix_web::error::ErrorBadRequest("Invalid recipe ID format")
    })?;

    let row = client
        .query_opt("SELECT id, title, ingredients, instructions FROM recipes WHERE id = $1", &[&uuid])
        .await
        .map_err(|e| {
            actix_web::error::ErrorInternalServerError(format!("Query error: {}", e))
        })?;

    let recipe_row = match row {
        Some(r) => r,
        None => return Err(actix_web::error::ErrorNotFound("Recipe not found")),
    };

    let id: Uuid = recipe_row.get(0);
    let title: String = recipe_row.get(1);
    let ingredients_json: serde_json::Value = recipe_row.get(2);
    let instructions: String = recipe_row.get(3);

    let ingredients: Vec<String> = serde_json::from_value(ingredients_json).map_err(|e| {
        actix_web::error::ErrorInternalServerError(format!("Ingredients parse error: {}", e))
    })?;

    let comment_rows = client
        .query("SELECT comment FROM comments WHERE recipe_id = $1 ORDER BY created_at", &[&uuid])
        .await
        .map_err(|e| {
            actix_web::error::ErrorInternalServerError(format!("Comments query error: {}", e))
        })?;

    let mut comments = Vec::new();
    for row in comment_rows {
        let comment: String = row.get(0);
        comments.push(Comment { comment });
    }

    let avg_rating_row = client
        .query_opt("SELECT AVG(rating) FROM ratings WHERE recipe_id = $1", &[&uuid])
        .await
        .map_err(|e| {
            actix_web::error::ErrorInternalServerError(format!("Ratings query error: {}", e))
        })?;

    let avg_rating: Option<f64> = avg_rating_row.and_then(|row| row.get(0));

    let mut html = format!(
        "<html><head><title>{}</title></head><body><h1>{}</h1>",
        title, title
    );
    html.push_str("<h2>Ingredients</h2><ul>");
    for ingredient in ingredients {
        html.push_str(&format!("<li>{}</li>", ingredient));
    }
    html.push_str("</ul>");
    html.push_str(&format!("<h2>Instructions</h2><p>{}</p>", instructions));
    html.push_str(&format!("<h2>Average Rating: {}</h2>", 
        avg_rating.map_or("No ratings yet".to_string(), |r| format!("{:.1}", r))));
    html.push_str("<h2>Comments</h2><ul>");
    for comment in comments {
        html.push_str(&format!("<li>{}</li>", comment.comment));
    }
    html.push_str("</ul></body></html>");

    Ok(HttpResponse::Ok().content_type("text/html").body(html))
}

async fn add_comment(
    pool: web::Data<Pool>,
    recipe_id: web::Path<String>,
    comment_req: web::Json<CommentRequest>,
) -> Result<impl Responder> {
    let client = pool.get().await.map_err(|e| {
        actix_web::error::ErrorInternalServerError(format!("Database connection error: {}", e))
    })?;

    let uuid = Uuid::parse_str(&recipe_id).map_err(|_| {
        actix_web::error::ErrorBadRequest("Invalid recipe ID format")
    })?;

    let exists: bool = client
        .query_opt("SELECT 1 FROM recipes WHERE id = $1", &[&uuid])
        .await
        .map_err(|e| {
            actix_web::error::ErrorInternalServerError(format!("Query error: {}", e))
        })?
        .is_some();

    if !exists {
        return Err(actix_web::error::ErrorNotFound("Recipe not found"));
    }

    client
        .execute(
            "INSERT INTO comments (recipe_id, comment) VALUES ($1, $2)",
            &[&uuid, &comment_req.comment],
        )
        .await
        .map_err(|e| {
            actix_web::error::ErrorInternalServerError(format!("Insert error: {}", e))
        })?;

    Ok(HttpResponse::Created().finish())
}

async fn add_rating(
    pool: web::Data<Pool>,
    recipe_id: web::Path<String>,
    rating_req: web::Json<RatingRequest>,
) -> Result<impl Responder> {
    if rating_req.rating < 1 || rating_req.rating > 5 {
        return Err(actix_web::error::ErrorBadRequest("Rating must be between 1 and 5"));
    }

    let client = pool.get().await.map_err(|e| {
        actix_web::error::ErrorInternalServerError(format!("Database connection error: {}", e))
    })?;

    let uuid = Uuid::parse_str(&recipe_id).map_err(|_| {
        actix_web::error::ErrorBadRequest("Invalid recipe ID format")
    })?;

    let exists: bool = client
        .query_opt("SELECT 1 FROM recipes WHERE id = $1", &[&uuid])
        .await
        .map_err(|e| {
            actix_web::error::ErrorInternalServerError(format!("Query error: {}", e))
        })?
        .is_some();

    if !exists {
        return Err(actix_web::error::ErrorNotFound("Recipe not found"));
    }

    client
        .execute(
            "INSERT INTO ratings (recipe_id, rating) VALUES ($1, $2)",
            &[&uuid, &(rating_req.rating as i16)],
        )
        .await
        .map_err(|e| {
            actix_web::error::ErrorInternalServerError(format!("Insert error: {}", e))
        })?;

    Ok(HttpResponse::Created().finish())
}

async fn initialize_database(pool: &Pool) -> std::result::Result<(), Box<dyn std::error::Error>> {
    let client = pool.get().await?;

    client.batch_execute(
        "
        CREATE TABLE IF NOT EXISTS recipes (
            id UUID PRIMARY KEY,
            title TEXT NOT NULL,
            ingredients JSONB NOT NULL,
            instructions TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        
        CREATE TABLE IF NOT EXISTS comments (
            id SERIAL PRIMARY KEY,
            recipe_id UUID REFERENCES recipes(id) ON DELETE CASCADE,
            comment TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        
        CREATE TABLE IF NOT EXISTS ratings (
            id SERIAL PRIMARY KEY,
            recipe_id UUID REFERENCES recipes(id) ON DELETE CASCADE,
            rating SMALLINT NOT NULL CHECK (rating >= 1 AND rating <= 5),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        ",
    ).await?;

    Ok(())
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let mut cfg = Config::new();
    cfg.host = Some(env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string()));
    cfg.port = Some(env::var("DB_PORT").unwrap_or_else(|_| "5432".to_string()).parse().unwrap());
    cfg.user = Some(env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string()));
    cfg.password = Some(env::var("DB_PASSWORD").unwrap_or_else(|_| "password".to_string()));
    cfg.dbname = Some(env::var("DB_NAME").unwrap_or_else(|_| "recipes".to_string()));
    cfg.manager = Some(ManagerConfig { recycling_method: RecyclingMethod::Fast });

    let pool = cfg.create_pool(Some(Runtime::Tokio1), tokio_postgres::NoTls)
        .expect("Failed to create database pool");

    initialize_database(&pool).await.expect("Failed to initialize database");

    let port = env::var("PORT").unwrap_or_else(|_| "5001".to_string()).parse().unwrap_or(5001);

    println!("Server starting on 0.0.0.0:{}", port);

    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(pool.clone()))
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