use actix_web::{web, App, HttpResponse, HttpServer, Responder, Result};
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

#[derive(Serialize)]
struct Recipe {
    id: String,
    title: String,
    ingredients: Vec<String>,
    instructions: String,
    comments: Vec<Comment>,
    avg_rating: Option<f64>,
}

#[derive(Serialize, Deserialize)]
struct Comment {
    comment: String,
}

#[derive(Deserialize)]
struct CommentRequest {
    comment: String,
}

#[derive(Deserialize)]
struct RatingRequest {
    rating: i32,
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
        .map_err(|e| actix_web::error::ErrorInternalServerError(e.to_string()))?;

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
    
    client
        .execute(
            "INSERT INTO recipes (id, title, ingredients, instructions) VALUES ($1, $2, $3, $4)",
            &[&id, &recipe.title, &recipe.ingredients, &recipe.instructions],
        )
        .await
        .map_err(|e| actix_web::error::ErrorBadRequest(e.to_string()))?;

    let response_recipe = Recipe {
        id: id.to_string(),
        title: recipe.title.clone(),
        ingredients: recipe.ingredients.clone(),
        instructions: recipe.instructions.clone(),
        comments: Vec::new(),
        avg_rating: None,
    };

    Ok(HttpResponse::Created().json(response_recipe))
}

async fn get_recipe(pool: web::Data<Pool>, recipe_id: web::Path<String>) -> Result<impl Responder> {
    let client = pool.get().await.map_err(|e| {
        actix_web::error::ErrorInternalServerError(format!("Database connection error: {}", e))
    })?;

    let uuid = Uuid::parse_str(&recipe_id).map_err(|_| actix_web::error::ErrorNotFound("Invalid recipe ID"))?;

    let row = client
        .query_opt("SELECT id, title, ingredients, instructions FROM recipes WHERE id = $1", &[&uuid])
        .await
        .map_err(|e| actix_web::error::ErrorInternalServerError(e.to_string()))?;

    let recipe_row = row.ok_or_else(|| actix_web::error::ErrorNotFound("Recipe not found"))?;

    let id: Uuid = recipe_row.get(0);
    let title: String = recipe_row.get(1);
    let ingredients: Vec<String> = recipe_row.get(2);
    let instructions: String = recipe_row.get(3);

    let comment_rows = client
        .query("SELECT comment FROM comments WHERE recipe_id = $1 ORDER BY created_at", &[&uuid])
        .await
        .map_err(|e| actix_web::error::ErrorInternalServerError(e.to_string()))?;

    let comments: Vec<Comment> = comment_rows
        .iter()
        .map(|row| Comment {
            comment: row.get(0),
        })
        .collect();

    let avg_rating_row = client
        .query_opt("SELECT AVG(rating) FROM ratings WHERE recipe_id = $1", &[&uuid])
        .await
        .map_err(|e| actix_web::error::ErrorInternalServerError(e.to_string()))?;

    let avg_rating: Option<f64> = avg_rating_row.and_then(|row| row.get(0));

    let recipe = Recipe {
        id: id.to_string(),
        title,
        ingredients,
        instructions,
        comments,
        avg_rating,
    };

    let html = format!(
        "<html><head><title>{}</title></head><body>
        <h1>{}</h1>
        <h2>Ingredients</h2>
        <ul>{}</ul>
        <h2>Instructions</h2>
        <p>{}</p>
        <h2>Average Rating: {}</h2>
        <h2>Comments</h2>
        <ul>{}</ul>
        </body></html>",
        recipe.title,
        recipe.title,
        recipe.ingredients.iter().map(|i| format!("<li>{}</li>", i)).collect::<String>(),
        recipe.instructions,
        recipe.avg_rating.map_or("No ratings yet".to_string(), |r| format!("{:.1}", r)),
        recipe.comments.iter().map(|c| format!("<li>{}</li>", c.comment)).collect::<String>()
    );

    Ok(HttpResponse::Ok().content_type("text/html").body(html))
}

async fn add_comment(
    pool: web::Data<Pool>,
    recipe_id: web::Path<String>,
    comment: web::Json<CommentRequest>,
) -> Result<impl Responder> {
    let client = pool.get().await.map_err(|e| {
        actix_web::error::ErrorInternalServerError(format!("Database connection error: {}", e))
    })?;

    let uuid = Uuid::parse_str(&recipe_id).map_err(|_| actix_web::error::ErrorNotFound("Invalid recipe ID"))?;

    let exists = client
        .query_opt("SELECT 1 FROM recipes WHERE id = $1", &[&uuid])
        .await
        .map_err(|e| actix_web::error::ErrorInternalServerError(e.to_string()))?;

    if exists.is_none() {
        return Err(actix_web::error::ErrorNotFound("Recipe not found"));
    }

    client
        .execute(
            "INSERT INTO comments (recipe_id, comment) VALUES ($1, $2)",
            &[&uuid, &comment.comment],
        )
        .await
        .map_err(|e| actix_web::error::ErrorBadRequest(e.to_string()))?;

    Ok(HttpResponse::Created().finish())
}

async fn add_rating(
    pool: web::Data<Pool>,
    recipe_id: web::Path<String>,
    rating: web::Json<RatingRequest>,
) -> Result<impl Responder> {
    if rating.rating < 1 || rating.rating > 5 {
        return Err(actix_web::error::ErrorBadRequest("Rating must be between 1 and 5"));
    }

    let client = pool.get().await.map_err(|e| {
        actix_web::error::ErrorInternalServerError(format!("Database connection error: {}", e))
    })?;

    let uuid = Uuid::parse_str(&recipe_id).map_err(|_| actix_web::error::ErrorNotFound("Invalid recipe ID"))?;

    let exists = client
        .query_opt("SELECT 1 FROM recipes WHERE id = $1", &[&uuid])
        .await
        .map_err(|e| actix_web::error::ErrorInternalServerError(e.to_string()))?;

    if exists.is_none() {
        return Err(actix_web::error::ErrorNotFound("Recipe not found"));
    }

    client
        .execute(
            "INSERT INTO ratings (recipe_id, rating) VALUES ($1, $2)",
            &[&uuid, &(rating.rating as i16)],
        )
        .await
        .map_err(|e| actix_web::error::ErrorBadRequest(e.to_string()))?;

    Ok(HttpResponse::Created().finish())
}

async fn initialize_database(pool: &Pool) -> std::result::Result<(), Box<dyn std::error::Error>> {
    let client = pool.get().await?;

    client.batch_execute(
        "
        CREATE TABLE IF NOT EXISTS recipes (
            id UUID PRIMARY KEY,
            title TEXT NOT NULL,
            ingredients TEXT[] NOT NULL,
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

    let db_host = env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string());
    let db_port = env::var("DB_PORT").unwrap_or_else(|_| "5432".to_string());
    let db_user = env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string());
    let db_password = env::var("DB_PASSWORD").unwrap_or_else(|_| "password".to_string());
    let db_name = env::var("DB_NAME").unwrap_or_else(|_| "recipes".to_string());

    let mut cfg = Config::new();
    cfg.host = Some(db_host);
    cfg.port = Some(db_port.parse().unwrap_or(5432));
    cfg.user = Some(db_user);
    cfg.password = Some(db_password);
    cfg.dbname = Some(db_name);
    cfg.manager = Some(ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    });

    let pool = cfg.create_pool(Some(Runtime::Tokio1), tokio_postgres::NoTls)
        .expect("Failed to create database pool");

    initialize_database(&pool).await.expect("Failed to initialize database");

    let port = env::var("PORT").unwrap_or_else(|_| "5001".to_string());
    let port = port.parse().unwrap_or(5001);

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