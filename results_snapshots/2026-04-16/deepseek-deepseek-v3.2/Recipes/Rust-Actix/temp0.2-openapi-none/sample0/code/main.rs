use actix_web::{web, App, HttpResponse, HttpServer, Responder, Result};
use deadpool_postgres::{Config, Pool, Runtime};
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

    let mut html = String::from("<html><body><h1>Recent Recipes</h1><ul>");
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

    let id = Uuid::parse_str(&recipe_id).map_err(|_| {
        actix_web::error::ErrorBadRequest("Invalid recipe ID format".to_string())
    })?;

    let row = client
        .query_opt("SELECT id, title, ingredients, instructions FROM recipes WHERE id = $1", &[&id])
        .await
        .map_err(|e| {
            actix_web::error::ErrorInternalServerError(format!("Query error: {}", e))
        })?;

    let row = row.ok_or_else(|| actix_web::error::ErrorNotFound("Recipe not found"))?;

    let id: Uuid = row.get(0);
    let title: String = row.get(1);
    let ingredients: serde_json::Value = row.get(2);
    let instructions: String = row.get(3);

    let ingredients_vec: Vec<String> = serde_json::from_value(ingredients).map_err(|e| {
        actix_web::error::ErrorInternalServerError(format!("Ingredients parse error: {}", e))
    })?;

    let comments_rows = client
        .query("SELECT comment FROM comments WHERE recipe_id = $1 ORDER BY created_at", &[&id])
        .await
        .map_err(|e| {
            actix_web::error::ErrorInternalServerError(format!("Comments query error: {}", e))
        })?;

    let mut comments = Vec::new();
    for comment_row in comments_rows {
        let comment: String = comment_row.get(0);
        comments.push(Comment { comment });
    }

    let avg_rating_row = client
        .query_opt(
            "SELECT AVG(rating) FROM ratings WHERE recipe_id = $1",
            &[&id],
        )
        .await
        .map_err(|e| {
            actix_web::error::ErrorInternalServerError(format!("Rating query error: {}", e))
        })?;

    let avg_rating: Option<f64> = avg_rating_row.and_then(|row| row.get(0));

    let mut html = format!(
        "<html><body><h1>{}</h1><h2>Ingredients</h2><ul>",
        title
    );
    for ingredient in &ingredients_vec {
        html.push_str(&format!("<li>{}</li>", ingredient));
    }
    html.push_str("</ul><h2>Instructions</h2><p>");
    html.push_str(&instructions);
    html.push_str("</p><h2>Comments</h2><ul>");
    for comment in &comments {
        html.push_str(&format!("<li>{}</li>", comment.comment));
    }
    html.push_str("</ul><h2>Average Rating: ");
    if let Some(rating) = avg_rating {
        html.push_str(&format!("{:.1}", rating));
    } else {
        html.push_str("No ratings yet");
    }
    html.push_str("</h2></body></html>");

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

    let id = Uuid::parse_str(&recipe_id).map_err(|_| {
        actix_web::error::ErrorBadRequest("Invalid recipe ID format".to_string())
    })?;

    let recipe_exists: bool = client
        .query_opt("SELECT 1 FROM recipes WHERE id = $1", &[&id])
        .await
        .map_err(|e| {
            actix_web::error::ErrorInternalServerError(format!("Query error: {}", e))
        })?
        .is_some();

    if !recipe_exists {
        return Err(actix_web::error::ErrorNotFound("Recipe not found"));
    }

    client
        .execute(
            "INSERT INTO comments (id, recipe_id, comment) VALUES ($1, $2, $3)",
            &[&Uuid::new_v4(), &id, &comment_req.comment],
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
        return Err(actix_web::error::ErrorBadRequest(
            "Rating must be between 1 and 5".to_string(),
        ));
    }

    let client = pool.get().await.map_err(|e| {
        actix_web::error::ErrorInternalServerError(format!("Database connection error: {}", e))
    })?;

    let id = Uuid::parse_str(&recipe_id).map_err(|_| {
        actix_web::error::ErrorBadRequest("Invalid recipe ID format".to_string())
    })?;

    let recipe_exists: bool = client
        .query_opt("SELECT 1 FROM recipes WHERE id = $1", &[&id])
        .await
        .map_err(|e| {
            actix_web::error::ErrorInternalServerError(format!("Query error: {}", e))
        })?
        .is_some();

    if !recipe_exists {
        return Err(actix_web::error::ErrorNotFound("Recipe not found"));
    }

    client
        .execute(
            "INSERT INTO ratings (id, recipe_id, rating) VALUES ($1, $2, $3)",
            &[&Uuid::new_v4(), &id, &(rating_req.rating as i32)],
        )
        .await
        .map_err(|e| {
            actix_web::error::ErrorInternalServerError(format!("Insert error: {}", e))
        })?;

    Ok(HttpResponse::Created().finish())
}

async fn initialize_database(pool: &Pool) -> std::result::Result<(), Box<dyn std::error::Error>> {
    let client = pool.get().await?;

    client
        .batch_execute(
            "
            CREATE TABLE IF NOT EXISTS recipes (
                id UUID PRIMARY KEY,
                title TEXT NOT NULL,
                ingredients JSONB NOT NULL,
                instructions TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS comments (
                id UUID PRIMARY KEY,
                recipe_id UUID REFERENCES recipes(id) ON DELETE CASCADE,
                comment TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ratings (
                id UUID PRIMARY KEY,
                recipe_id UUID REFERENCES recipes(id) ON DELETE CASCADE,
                rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            ",
        )
        .await?;

    Ok(())
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let db_host = env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string());
    let db_port = env::var("DB_PORT")
        .unwrap_or_else(|_| "5432".to_string())
        .parse::<u16>()
        .unwrap_or(5432);
    let db_user = env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string());
    let db_password = env::var("DB_PASSWORD").unwrap_or_else(|_| "password".to_string());
    let db_name = env::var("DB_NAME").unwrap_or_else(|_| "recipe_db".to_string());

    let mut cfg = Config::new();
    cfg.host = Some(db_host);
    cfg.port = Some(db_port);
    cfg.user = Some(db_user);
    cfg.password = Some(db_password);
    cfg.dbname = Some(db_name);

    let pool = cfg.create_pool(Some(Runtime::Tokio1), tokio_postgres::NoTls).unwrap();

    initialize_database(&pool)
        .await
        .expect("Failed to initialize database");

    let port = env::var("PORT")
        .unwrap_or_else(|_| "5001".to_string())
        .parse::<u16>()
        .unwrap_or(5001);

    println!("Server starting on 0.0.0.0:{}", port);

    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(pool.clone()))
            .route("/recipes", web::get().to(get_recipes_overview))
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
    .bind(("0.0.0.0", port))?
    .run()
    .await
}