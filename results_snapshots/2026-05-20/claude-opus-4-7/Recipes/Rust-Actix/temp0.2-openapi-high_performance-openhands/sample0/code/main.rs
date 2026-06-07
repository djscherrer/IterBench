use actix_web::{web, App, HttpServer, HttpResponse, Responder};
use deadpool_postgres::{Config, ManagerConfig, Pool, RecyclingMethod, Runtime};
use serde::{Deserialize, Serialize};
use std::env;
use tokio_postgres::NoTls;
use uuid::Uuid;

#[derive(Deserialize)]
struct UploadRecipeRequest {
    title: String,
    ingredients: Vec<String>,
    instructions: String,
}

#[derive(Serialize)]
struct CommentObj {
    comment: String,
}

#[derive(Serialize)]
struct Recipe {
    id: String,
    title: String,
    ingredients: Vec<String>,
    instructions: String,
    comments: Vec<CommentObj>,
    #[serde(rename = "avgRating")]
    avg_rating: Option<f64>,
}

#[derive(Deserialize)]
struct CommentRequest {
    comment: String,
}

#[derive(Deserialize)]
struct RatingRequest {
    rating: i32,
}

fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}

async fn init_db(pool: &Pool) -> Result<(), Box<dyn std::error::Error>> {
    let client = pool.get().await?;
    // pgcrypto first (best effort)
    let _ = client
        .batch_execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
        .await;
    client
        .batch_execute(
            "
        CREATE TABLE IF NOT EXISTS recipes (
            id UUID PRIMARY KEY,
            title TEXT NOT NULL,
            ingredients TEXT[] NOT NULL,
            instructions TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS comments (
            id BIGSERIAL PRIMARY KEY,
            recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            comment TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_comments_recipe_id ON comments(recipe_id);
        CREATE TABLE IF NOT EXISTS ratings (
            id BIGSERIAL PRIMARY KEY,
            recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            rating SMALLINT NOT NULL CHECK (rating >= 1 AND rating <= 5),
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id ON ratings(recipe_id);
        CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes(created_at DESC);
        ",
        )
        .await?;
    Ok(())
}

async fn get_recipes_overview(pool: web::Data<Pool>) -> impl Responder {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().body("Server error"),
    };
    let rows = match client
        .query(
            "SELECT r.id, r.title
             FROM recipes r
             ORDER BY r.created_at DESC LIMIT 100",
            &[],
        )
        .await
    {
        Ok(r) => r,
        Err(_) => return HttpResponse::InternalServerError().body("Server error"),
    };

    let mut html = String::from(
        "<!DOCTYPE html><html><head><title>Recipes</title></head><body><h1>Recipes</h1><ul>",
    );
    for row in rows {
        let id: Uuid = row.get(0);
        let title: String = row.get(1);
        html.push_str(&format!(
            "<li><a href=\"/recipes/{}\">{}</a></li>",
            id,
            html_escape(&title)
        ));
    }
    html.push_str("</ul></body></html>");
    HttpResponse::Ok()
        .content_type("text/html; charset=utf-8")
        .body(html)
}

async fn upload_recipe(
    pool: web::Data<Pool>,
    body: web::Json<UploadRecipeRequest>,
) -> impl Responder {
    let req = body.into_inner();
    if req.title.trim().is_empty()
        || req.ingredients.is_empty()
        || req.instructions.trim().is_empty()
    {
        return HttpResponse::BadRequest().json(serde_json::json!({"error": "Invalid input"}));
    }
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().body("Server error"),
    };
    let id = Uuid::new_v4();
    let res = client
        .execute(
            "INSERT INTO recipes (id, title, ingredients, instructions) VALUES ($1, $2, $3, $4)",
            &[&id, &req.title, &req.ingredients, &req.instructions],
        )
        .await;
    if res.is_err() {
        return HttpResponse::InternalServerError().body("Server error");
    }
    let recipe = Recipe {
        id: id.to_string(),
        title: req.title,
        ingredients: req.ingredients,
        instructions: req.instructions,
        comments: vec![],
        avg_rating: None,
    };
    HttpResponse::Created().json(recipe)
}

async fn get_recipe(pool: web::Data<Pool>, path: web::Path<String>) -> impl Responder {
    let recipe_id_str = path.into_inner();
    let recipe_id = match Uuid::parse_str(&recipe_id_str) {
        Ok(u) => u,
        Err(_) => return HttpResponse::NotFound().body("Recipe not found"),
    };
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().body("Server error"),
    };
    let recipe_row = match client
        .query_opt(
            "SELECT id, title, ingredients, instructions FROM recipes WHERE id = $1",
            &[&recipe_id],
        )
        .await
    {
        Ok(r) => r,
        Err(_) => return HttpResponse::InternalServerError().body("Server error"),
    };
    let row = match recipe_row {
        Some(r) => r,
        None => return HttpResponse::NotFound().body("Recipe not found"),
    };
    let title: String = row.get(1);
    let ingredients: Vec<String> = row.get(2);
    let instructions: String = row.get(3);

    let comments = client
        .query(
            "SELECT comment FROM comments WHERE recipe_id = $1 ORDER BY created_at ASC",
            &[&recipe_id],
        )
        .await
        .unwrap_or_default();
    let avg_row = client
        .query_one(
            "SELECT AVG(rating)::float8 FROM ratings WHERE recipe_id = $1",
            &[&recipe_id],
        )
        .await
        .ok();
    let avg_rating: Option<f64> = avg_row.and_then(|r| r.get::<_, Option<f64>>(0));

    let mut html = String::new();
    html.push_str("<!DOCTYPE html><html><head><title>");
    html.push_str(&html_escape(&title));
    html.push_str("</title></head><body>");
    html.push_str(&format!("<h1>{}</h1>", html_escape(&title)));
    html.push_str("<h2>Ingredients</h2><ul>");
    for ing in &ingredients {
        html.push_str(&format!("<li>{}</li>", html_escape(ing)));
    }
    html.push_str("</ul><h2>Instructions</h2><p>");
    html.push_str(&html_escape(&instructions));
    html.push_str("</p>");
    html.push_str(&format!(
        "<h2>Average Rating</h2><p>{}</p>",
        match avg_rating {
            Some(r) => format!("{:.2}", r),
            None => "No ratings yet".to_string(),
        }
    ));
    html.push_str("<h2>Comments</h2><ul>");
    for c in comments {
        let comment: String = c.get(0);
        html.push_str(&format!("<li>{}</li>", html_escape(&comment)));
    }
    html.push_str("</ul></body></html>");

    HttpResponse::Ok()
        .content_type("text/html; charset=utf-8")
        .body(html)
}

async fn add_comment(
    pool: web::Data<Pool>,
    path: web::Path<String>,
    body: web::Json<CommentRequest>,
) -> impl Responder {
    let recipe_id_str = path.into_inner();
    let recipe_id = match Uuid::parse_str(&recipe_id_str) {
        Ok(u) => u,
        Err(_) => return HttpResponse::NotFound().body("Recipe not found"),
    };
    let req = body.into_inner();
    if req.comment.trim().is_empty() {
        return HttpResponse::BadRequest().json(serde_json::json!({"error": "Invalid input"}));
    }
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().body("Server error"),
    };
    let exists = client
        .query_opt("SELECT 1 FROM recipes WHERE id = $1", &[&recipe_id])
        .await
        .unwrap_or(None);
    if exists.is_none() {
        return HttpResponse::NotFound().body("Recipe not found");
    }
    let res = client
        .execute(
            "INSERT INTO comments (recipe_id, comment) VALUES ($1, $2)",
            &[&recipe_id, &req.comment],
        )
        .await;
    if res.is_err() {
        return HttpResponse::InternalServerError().body("Server error");
    }
    HttpResponse::Created().json(serde_json::json!({"message": "Comment added"}))
}

async fn add_rating(
    pool: web::Data<Pool>,
    path: web::Path<String>,
    body: web::Json<RatingRequest>,
) -> impl Responder {
    let recipe_id_str = path.into_inner();
    let recipe_id = match Uuid::parse_str(&recipe_id_str) {
        Ok(u) => u,
        Err(_) => return HttpResponse::NotFound().body("Recipe not found"),
    };
    let req = body.into_inner();
    if req.rating < 1 || req.rating > 5 {
        return HttpResponse::BadRequest().json(serde_json::json!({"error": "Invalid input"}));
    }
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().body("Server error"),
    };
    let exists = client
        .query_opt("SELECT 1 FROM recipes WHERE id = $1", &[&recipe_id])
        .await
        .unwrap_or(None);
    if exists.is_none() {
        return HttpResponse::NotFound().body("Recipe not found");
    }
    let rating_i16: i16 = req.rating as i16;
    let res = client
        .execute(
            "INSERT INTO ratings (recipe_id, rating) VALUES ($1, $2)",
            &[&recipe_id, &rating_i16],
        )
        .await;
    if res.is_err() {
        return HttpResponse::InternalServerError().body("Server error");
    }
    HttpResponse::Created().json(serde_json::json!({"message": "Rating added"}))
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let db_host = env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string());
    let db_port: u16 = env::var("DB_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(5432);
    let db_user = env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string());
    let db_password = env::var("DB_PASSWORD").unwrap_or_else(|_| "postgres".to_string());
    let db_name = env::var("DB_NAME").unwrap_or_else(|_| "postgres".to_string());

    let mut cfg = Config::new();
    cfg.host = Some(db_host);
    cfg.port = Some(db_port);
    cfg.user = Some(db_user);
    cfg.password = Some(db_password);
    cfg.dbname = Some(db_name);
    cfg.manager = Some(ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    });
    let pool_size: usize = env::var("DB_POOL_SIZE")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(32);
    let mut pool_cfg = deadpool_postgres::PoolConfig::default();
    pool_cfg.max_size = pool_size;
    cfg.pool = Some(pool_cfg);

    let pool = cfg
        .create_pool(Some(Runtime::Tokio1), NoTls)
        .expect("Failed to create pool");

    for attempt in 0..30 {
        match init_db(&pool).await {
            Ok(_) => break,
            Err(e) => {
                eprintln!("DB init attempt {} failed: {}", attempt, e);
                if attempt == 29 {
                    panic!("Failed to initialize database");
                }
                tokio::time::sleep(std::time::Duration::from_secs(1)).await;
            }
        }
    }

    let port: u16 = env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(5001);

    let pool_data = web::Data::new(pool);

    HttpServer::new(move || {
        let json_cfg = web::JsonConfig::default()
            .limit(1024 * 1024)
            .error_handler(|err, _req| {
                let resp = HttpResponse::BadRequest()
                    .json(serde_json::json!({"error": "Invalid input"}));
                actix_web::error::InternalError::from_response(err, resp).into()
            });

        App::new()
            .app_data(pool_data.clone())
            .app_data(json_cfg)
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
