use actix_web::{web, App, HttpServer, HttpResponse};
use deadpool_postgres::{Config, Pool, Runtime, ManagerConfig, RecyclingMethod};
use serde::{Deserialize, Serialize};
use tokio_postgres::NoTls;
use uuid::Uuid;
use std::sync::Arc;
use tokio::sync::RwLock;
use std::collections::HashMap;
use std::time::Instant;

#[derive(Serialize, Deserialize, Clone)]
struct Recipe {
    id: String,
    title: String,
    ingredients: Vec<String>,
    instructions: String,
    comments: Vec<CommentEntry>,
    #[serde(rename = "avgRating")]
    avg_rating: Option<f64>,
}

#[derive(Serialize, Deserialize, Clone)]
struct CommentEntry {
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

struct CacheEntry<T> {
    data: T,
    created: Instant,
}

struct AppCache {
    overview: RwLock<Option<CacheEntry<String>>>,
    recipes: RwLock<HashMap<String, CacheEntry<String>>>,
}

impl AppCache {
    fn new() -> Self {
        Self {
            overview: RwLock::new(None),
            recipes: RwLock::new(HashMap::new()),
        }
    }
}

const CACHE_TTL_MS: u128 = 2000;

async fn init_db(pool: &Pool) {
    let client = pool.get().await.expect("Failed to get DB connection");
    client.batch_execute("
        CREATE TABLE IF NOT EXISTS recipes (
            id UUID PRIMARY KEY,
            title TEXT NOT NULL,
            ingredients TEXT[] NOT NULL,
            instructions TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS comments (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            recipe_id UUID NOT NULL REFERENCES recipes(id),
            comment TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS ratings (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            recipe_id UUID NOT NULL REFERENCES recipes(id),
            rating SMALLINT NOT NULL CHECK (rating >= 1 AND rating <= 5),
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_comments_recipe_id ON comments(recipe_id);
        CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id ON ratings(recipe_id);
        CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes(created_at DESC);
    ").await.expect("Failed to initialize database");
}

async fn get_recipes_overview(
    pool: web::Data<Pool>,
    cache: web::Data<Arc<AppCache>>,
) -> HttpResponse {
    // Check cache
    {
        let cached = cache.overview.read().await;
        if let Some(entry) = cached.as_ref() {
            if entry.created.elapsed().as_millis() < CACHE_TTL_MS {
                return HttpResponse::Ok().content_type("text/html").body(entry.data.clone());
            }
        }
    }

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let rows = match client.query(
        "SELECT r.id, r.title, COALESCE(AVG(rt.rating), 0) as avg_rating
         FROM recipes r
         LEFT JOIN ratings rt ON r.id = rt.recipe_id
         GROUP BY r.id, r.title, r.created_at
         ORDER BY r.created_at DESC
         LIMIT 50",
        &[],
    ).await {
        Ok(rows) => rows,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let mut recent_html = String::new();
    let mut rated: Vec<(String, String, f64)> = Vec::new();

    for row in &rows {
        let id: Uuid = row.get(0);
        let title: &str = row.get(1);
        let avg: f64 = row.get::<_, f64>(2);
        let id_str = id.to_string();

        recent_html.push_str(&format!(
            "<li><a href=\"/recipes/{}\">{}</a></li>\n",
            html_escape(&id_str),
            html_escape(title)
        ));
        rated.push((id_str, title.to_string(), avg));
    }

    rated.sort_by(|a, b| b.2.partial_cmp(&a.2).unwrap_or(std::cmp::Ordering::Equal));

    let mut top_html = String::new();
    for (id_str, title, avg) in rated.iter().take(10) {
        top_html.push_str(&format!(
            "<li><a href=\"/recipes/{}\">{}</a> (Rating: {:.1})</li>\n",
            html_escape(id_str),
            html_escape(title),
            avg
        ));
    }

    let html = format!(
        "<!DOCTYPE html><html><head><title>Recipes</title></head><body>\
        <h1>Recipes Overview</h1>\
        <h2>Recent Recipes</h2><ul>{}</ul>\
        <h2>Top Rated Recipes</h2><ul>{}</ul>\
        </body></html>",
        recent_html, top_html
    );

    // Update cache
    {
        let mut cached = cache.overview.write().await;
        *cached = Some(CacheEntry { data: html.clone(), created: Instant::now() });
    }

    HttpResponse::Ok().content_type("text/html").body(html)
}

async fn upload_recipe(
    pool: web::Data<Pool>,
    cache: web::Data<Arc<AppCache>>,
    body: web::Json<UploadRecipeRequest>,
) -> HttpResponse {
    if body.title.is_empty() || body.ingredients.is_empty() || body.instructions.is_empty() {
        return HttpResponse::BadRequest().finish();
    }

    let id = Uuid::new_v4();
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let ingredients: Vec<&str> = body.ingredients.iter().map(|s| s.as_str()).collect();

    match client.execute(
        "INSERT INTO recipes (id, title, ingredients, instructions) VALUES ($1, $2, $3, $4)",
        &[&id, &body.title, &ingredients, &body.instructions],
    ).await {
        Ok(_) => {},
        Err(_) => return HttpResponse::InternalServerError().finish(),
    }

    // Invalidate overview cache
    {
        let mut cached = cache.overview.write().await;
        *cached = None;
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
    cache: web::Data<Arc<AppCache>>,
    path: web::Path<String>,
) -> HttpResponse {
    let recipe_id_str = path.into_inner();

    // Check cache
    {
        let cached = cache.recipes.read().await;
        if let Some(entry) = cached.get(&recipe_id_str) {
            if entry.created.elapsed().as_millis() < CACHE_TTL_MS {
                return HttpResponse::Ok().content_type("text/html").body(entry.data.clone());
            }
        }
    }

    let recipe_id = match Uuid::parse_str(&recipe_id_str) {
        Ok(id) => id,
        Err(_) => return HttpResponse::NotFound().finish(),
    };

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let row = match client.query_opt(
        "SELECT r.id, r.title, r.ingredients, r.instructions, COALESCE(AVG(rt.rating)::float8, 0) as avg_rating, COUNT(rt.rating) as rating_count
         FROM recipes r
         LEFT JOIN ratings rt ON r.id = rt.recipe_id
         WHERE r.id = $1
         GROUP BY r.id",
        &[&recipe_id],
    ).await {
        Ok(Some(row)) => row,
        Ok(None) => return HttpResponse::NotFound().finish(),
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let title: &str = row.get(1);
    let ingredients: Vec<String> = row.get(2);
    let instructions: &str = row.get(3);
    let avg_rating: f64 = row.get(4);
    let rating_count: i64 = row.get(5);

    let comment_rows = match client.query(
        "SELECT comment FROM comments WHERE recipe_id = $1 ORDER BY created_at ASC",
        &[&recipe_id],
    ).await {
        Ok(rows) => rows,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let mut ingredients_html = String::new();
    for ing in &ingredients {
        ingredients_html.push_str(&format!("<li>{}</li>", html_escape(ing)));
    }

    let mut comments_html = String::new();
    for crow in &comment_rows {
        let comment: &str = crow.get(0);
        comments_html.push_str(&format!("<li>{}</li>", html_escape(comment)));
    }

    let rating_display = if rating_count > 0 {
        format!("{:.1}", avg_rating)
    } else {
        "No ratings yet".to_string()
    };

    let html = format!(
        "<!DOCTYPE html><html><head><title>{}</title></head><body>\
        <h1>{}</h1>\
        <h2>Ingredients</h2><ul>{}</ul>\
        <h2>Instructions</h2><p>{}</p>\
        <h2>Average Rating</h2><p>{}</p>\
        <h2>Comments</h2><ul>{}</ul>\
        </body></html>",
        html_escape(title),
        html_escape(title),
        ingredients_html,
        html_escape(instructions),
        rating_display,
        comments_html
    );

    // Update cache
    {
        let mut cached = cache.recipes.write().await;
        cached.insert(recipe_id_str, CacheEntry { data: html.clone(), created: Instant::now() });
    }

    HttpResponse::Ok().content_type("text/html").body(html)
}

async fn add_comment(
    pool: web::Data<Pool>,
    cache: web::Data<Arc<AppCache>>,
    path: web::Path<String>,
    body: web::Json<AddCommentRequest>,
) -> HttpResponse {
    if body.comment.is_empty() {
        return HttpResponse::BadRequest().finish();
    }

    let recipe_id_str = path.into_inner();
    let recipe_id = match Uuid::parse_str(&recipe_id_str) {
        Ok(id) => id,
        Err(_) => return HttpResponse::NotFound().finish(),
    };

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    // Check recipe exists
    let exists = match client.query_opt(
        "SELECT 1 FROM recipes WHERE id = $1",
        &[&recipe_id],
    ).await {
        Ok(Some(_)) => true,
        Ok(None) => false,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    if !exists {
        return HttpResponse::NotFound().finish();
    }

    match client.execute(
        "INSERT INTO comments (recipe_id, comment) VALUES ($1, $2)",
        &[&recipe_id, &body.comment],
    ).await {
        Ok(_) => {},
        Err(_) => return HttpResponse::InternalServerError().finish(),
    }

    // Invalidate recipe cache
    {
        let mut cached = cache.recipes.write().await;
        cached.remove(&recipe_id_str);
    }

    HttpResponse::Created().finish()
}

async fn add_rating(
    pool: web::Data<Pool>,
    cache: web::Data<Arc<AppCache>>,
    path: web::Path<String>,
    body: web::Json<AddRatingRequest>,
) -> HttpResponse {
    if body.rating < 1 || body.rating > 5 {
        return HttpResponse::BadRequest().finish();
    }

    let recipe_id_str = path.into_inner();
    let recipe_id = match Uuid::parse_str(&recipe_id_str) {
        Ok(id) => id,
        Err(_) => return HttpResponse::NotFound().finish(),
    };

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let exists = match client.query_opt(
        "SELECT 1 FROM recipes WHERE id = $1",
        &[&recipe_id],
    ).await {
        Ok(Some(_)) => true,
        Ok(None) => false,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    if !exists {
        return HttpResponse::NotFound().finish();
    }

    let rating_i16 = body.rating as i16;
    match client.execute(
        "INSERT INTO ratings (recipe_id, rating) VALUES ($1, $2)",
        &[&recipe_id, &rating_i16],
    ).await {
        Ok(_) => {},
        Err(_) => return HttpResponse::InternalServerError().finish(),
    }

    // Invalidate caches
    {
        let mut cached = cache.recipes.write().await;
        cached.remove(&recipe_id_str);
    }
    {
        let mut cached = cache.overview.write().await;
        *cached = None;
    }

    HttpResponse::Created().finish()
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
    let db_port: u16 = std::env::var("DB_PORT").unwrap_or_else(|_| "5432".to_string()).parse().unwrap_or(5432);
    let db_user = std::env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string());
    let db_password = std::env::var("DB_PASSWORD").unwrap_or_else(|_| "postgres".to_string());
    let db_name = std::env::var("DB_NAME").unwrap_or_else(|_| "testdb".to_string());
    let port: u16 = std::env::var("PORT").unwrap_or_else(|_| "5001".to_string()).parse().unwrap_or(5001);

    let mut cfg = Config::new();
    cfg.host = Some(db_host);
    cfg.port = Some(db_port);
    cfg.user = Some(db_user);
    cfg.password = Some(db_password);
    cfg.dbname = Some(db_name);
    cfg.manager = Some(ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    });

    let pool = cfg.create_pool(Some(Runtime::Tokio1), NoTls).expect("Failed to create pool");

    init_db(&pool).await;

    let cache = Arc::new(AppCache::new());

    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(pool.clone()))
            .app_data(web::Data::new(cache.clone()))
            .app_data(web::JsonConfig::default().limit(1_048_576))
            .route("/recipes", web::get().to(get_recipes_overview))
            .route("/recipes/upload", web::post().to(upload_recipe))
            .route("/recipes/{recipeId}", web::get().to(get_recipe))
            .route("/recipes/{recipeId}/comments", web::post().to(add_comment))
            .route("/recipes/{recipeId}/ratings", web::post().to(add_rating))
    })
    .workers(num_cpus())
    .bind(("0.0.0.0", port))?
    .run()
    .await
}

fn num_cpus() -> usize {
    std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4)
}
