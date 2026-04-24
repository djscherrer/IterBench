use actix_web::{web, App, HttpServer, HttpResponse};
use deadpool_postgres::{Config, Pool, Runtime};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tokio::sync::RwLock;
use tokio_postgres::NoTls;
use uuid::Uuid;
use std::collections::HashMap;
use std::time::Instant;

#[derive(Debug, Serialize, Deserialize, Clone)]
struct Recipe {
    id: String,
    title: String,
    ingredients: Vec<String>,
    instructions: String,
    comments: Vec<CommentEntry>,
    #[serde(rename = "avgRating")]
    avg_rating: Option<f64>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct CommentEntry {
    comment: String,
}

#[derive(Debug, Deserialize)]
struct UploadRecipeRequest {
    title: String,
    ingredients: Vec<String>,
    instructions: String,
}

#[derive(Debug, Deserialize)]
struct AddCommentRequest {
    comment: String,
}

#[derive(Debug, Deserialize)]
struct AddRatingRequest {
    rating: i32,
}

#[derive(Clone)]
struct CachedOverview {
    html: String,
    generated_at: Instant,
}

struct AppState {
    pool: Pool,
    overview_cache: RwLock<Option<CachedOverview>>,
    recipe_cache: RwLock<HashMap<String, (Recipe, Instant)>>,
}

const OVERVIEW_CACHE_TTL_MS: u128 = 500;
const RECIPE_CACHE_TTL_MS: u128 = 500;

async fn init_db(pool: &Pool) {
    let client = pool.get().await.expect("Failed to get DB connection for init");
    client
        .batch_execute(
            "
            CREATE TABLE IF NOT EXISTS recipes (
                id UUID PRIMARY KEY,
                title TEXT NOT NULL,
                ingredients TEXT[] NOT NULL,
                instructions TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS comments (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                comment TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS ratings (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                rating SMALLINT NOT NULL CHECK (rating >= 1 AND rating <= 5),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_comments_recipe_id ON comments(recipe_id);
            CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id ON ratings(recipe_id);
            CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes(created_at DESC);
            ",
        )
        .await
        .expect("Failed to initialize database tables");
}

async fn get_recipes_overview(data: web::Data<Arc<AppState>>) -> HttpResponse {
    // Check cache
    {
        let cache = data.overview_cache.read().await;
        if let Some(ref cached) = *cache {
            if cached.generated_at.elapsed().as_millis() < OVERVIEW_CACHE_TTL_MS {
                return HttpResponse::Ok().content_type("text/html").body(cached.html.clone());
            }
        }
    }

    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    // Get recent recipes (last 10)
    let recent_rows = match client
        .query(
            "SELECT id, title FROM recipes ORDER BY created_at DESC LIMIT 10",
            &[],
        )
        .await
    {
        Ok(r) => r,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    // Get top-rated recipes (top 10)
    let top_rows = match client
        .query(
            "SELECT r.id, r.title, AVG(rt.rating)::DOUBLE PRECISION as avg_rating
             FROM recipes r
             JOIN ratings rt ON r.id = rt.recipe_id
             GROUP BY r.id, r.title
             ORDER BY avg_rating DESC
             LIMIT 10",
            &[],
        )
        .await
    {
        Ok(r) => r,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let mut html = String::from("<!DOCTYPE html><html><head><title>Recipe Overview</title></head><body>");
    html.push_str("<h1>Recipes</h1>");

    html.push_str("<h2>Recent Recipes</h2><ul>");
    for row in &recent_rows {
        let id: Uuid = row.get(0);
        let title: &str = row.get(1);
        html.push_str(&format!(
            "<li><a href=\"/recipes/{}\">{}</a></li>",
            id,
            html_escape(title)
        ));
    }
    html.push_str("</ul>");

    html.push_str("<h2>Top Rated Recipes</h2><ul>");
    for row in &top_rows {
        let id: Uuid = row.get(0);
        let title: &str = row.get(1);
        let avg: f64 = row.get(2);
        html.push_str(&format!(
            "<li><a href=\"/recipes/{}\">{}</a> (Rating: {:.1})</li>",
            id,
            html_escape(title),
            avg
        ));
    }
    html.push_str("</ul>");
    html.push_str("</body></html>");

    // Update cache
    {
        let mut cache = data.overview_cache.write().await;
        *cache = Some(CachedOverview {
            html: html.clone(),
            generated_at: Instant::now(),
        });
    }

    HttpResponse::Ok().content_type("text/html").body(html)
}

async fn upload_recipe(
    data: web::Data<Arc<AppState>>,
    body: web::Json<UploadRecipeRequest>,
) -> HttpResponse {
    if body.title.is_empty() || body.ingredients.is_empty() || body.instructions.is_empty() {
        return HttpResponse::BadRequest().finish();
    }

    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let id = Uuid::new_v4();
    let ingredients: Vec<&str> = body.ingredients.iter().map(|s| s.as_str()).collect();

    match client
        .execute(
            "INSERT INTO recipes (id, title, ingredients, instructions) VALUES ($1, $2, $3, $4)",
            &[&id, &body.title, &ingredients, &body.instructions],
        )
        .await
    {
        Ok(_) => {}
        Err(_) => return HttpResponse::InternalServerError().finish(),
    }

    // Invalidate overview cache
    {
        let mut cache = data.overview_cache.write().await;
        *cache = None;
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
    data: web::Data<Arc<AppState>>,
    path: web::Path<String>,
) -> HttpResponse {
    let recipe_id_str = path.into_inner();

    // Check cache
    {
        let cache = data.recipe_cache.read().await;
        if let Some((recipe, instant)) = cache.get(&recipe_id_str) {
            if instant.elapsed().as_millis() < RECIPE_CACHE_TTL_MS {
                let html = render_recipe_html(recipe);
                return HttpResponse::Ok().content_type("text/html").body(html);
            }
        }
    }

    let recipe_id = match Uuid::parse_str(&recipe_id_str) {
        Ok(id) => id,
        Err(_) => return HttpResponse::NotFound().finish(),
    };

    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let row = match client
        .query_opt(
            "SELECT id, title, ingredients, instructions FROM recipes WHERE id = $1",
            &[&recipe_id],
        )
        .await
    {
        Ok(Some(r)) => r,
        Ok(None) => return HttpResponse::NotFound().finish(),
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let id: Uuid = row.get(0);
    let title: String = row.get(1);
    let ingredients: Vec<String> = row.get(2);
    let instructions: String = row.get(3);

    let comment_rows = match client
        .query(
            "SELECT comment FROM comments WHERE recipe_id = $1 ORDER BY created_at ASC",
            &[&recipe_id],
        )
        .await
    {
        Ok(r) => r,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let comments: Vec<CommentEntry> = comment_rows
        .iter()
        .map(|r| CommentEntry {
            comment: r.get(0),
        })
        .collect();

    let avg_row = match client
        .query_opt(
            "SELECT AVG(rating)::DOUBLE PRECISION FROM ratings WHERE recipe_id = $1",
            &[&recipe_id],
        )
        .await
    {
        Ok(r) => r,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let avg_rating: Option<f64> = avg_row.and_then(|r| r.get(0));

    let recipe = Recipe {
        id: id.to_string(),
        title,
        ingredients,
        instructions,
        comments,
        avg_rating,
    };

    let html = render_recipe_html(&recipe);

    // Update cache
    {
        let mut cache = data.recipe_cache.write().await;
        cache.insert(recipe_id_str, (recipe, Instant::now()));
    }

    HttpResponse::Ok().content_type("text/html").body(html)
}

async fn add_comment(
    data: web::Data<Arc<AppState>>,
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

    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    // Check recipe exists
    let exists = match client
        .query_opt("SELECT 1 FROM recipes WHERE id = $1", &[&recipe_id])
        .await
    {
        Ok(r) => r.is_some(),
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    if !exists {
        return HttpResponse::NotFound().finish();
    }

    match client
        .execute(
            "INSERT INTO comments (recipe_id, comment) VALUES ($1, $2)",
            &[&recipe_id, &body.comment],
        )
        .await
    {
        Ok(_) => {}
        Err(_) => return HttpResponse::InternalServerError().finish(),
    }

    // Invalidate recipe cache
    {
        let mut cache = data.recipe_cache.write().await;
        cache.remove(&recipe_id_str);
    }

    HttpResponse::Created().finish()
}

async fn add_rating(
    data: web::Data<Arc<AppState>>,
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

    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    // Check recipe exists
    let exists = match client
        .query_opt("SELECT 1 FROM recipes WHERE id = $1", &[&recipe_id])
        .await
    {
        Ok(r) => r.is_some(),
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    if !exists {
        return HttpResponse::NotFound().finish();
    }

    let rating_i16 = body.rating as i16;
    match client
        .execute(
            "INSERT INTO ratings (recipe_id, rating) VALUES ($1, $2)",
            &[&recipe_id, &rating_i16],
        )
        .await
    {
        Ok(_) => {}
        Err(_) => return HttpResponse::InternalServerError().finish(),
    }

    // Invalidate caches
    {
        let mut cache = data.recipe_cache.write().await;
        cache.remove(&recipe_id_str);
    }
    {
        let mut cache = data.overview_cache.write().await;
        *cache = None;
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

fn render_recipe_html(recipe: &Recipe) -> String {
    let mut html = String::from("<!DOCTYPE html><html><head><title>");
    html.push_str(&html_escape(&recipe.title));
    html.push_str("</title></head><body>");
    html.push_str(&format!("<h1>{}</h1>", html_escape(&recipe.title)));

    html.push_str("<h2>Ingredients</h2><ul>");
    for ing in &recipe.ingredients {
        html.push_str(&format!("<li>{}</li>", html_escape(ing)));
    }
    html.push_str("</ul>");

    html.push_str("<h2>Instructions</h2>");
    html.push_str(&format!("<p>{}</p>", html_escape(&recipe.instructions)));

    html.push_str("<h2>Rating</h2>");
    match recipe.avg_rating {
        Some(r) => html.push_str(&format!("<p>Average Rating: {:.1}/5</p>", r)),
        None => html.push_str("<p>No ratings yet</p>"),
    }

    html.push_str("<h2>Comments</h2>");
    if recipe.comments.is_empty() {
        html.push_str("<p>No comments yet</p>");
    } else {
        html.push_str("<ul>");
        for c in &recipe.comments {
            html.push_str(&format!("<li>{}</li>", html_escape(&c.comment)));
        }
        html.push_str("</ul>");
    }

    html.push_str("</body></html>");
    html
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let db_host = std::env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string());
    let db_port = std::env::var("DB_PORT")
        .unwrap_or_else(|_| "5432".to_string())
        .parse::<u16>()
        .unwrap_or(5432);
    let db_user = std::env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string());
    let db_password = std::env::var("DB_PASSWORD").unwrap_or_else(|_| "postgres".to_string());
    let db_name = std::env::var("DB_NAME").unwrap_or_else(|_| "testdb".to_string());

    let mut cfg = Config::new();
    cfg.host = Some(db_host);
    cfg.port = Some(db_port);
    cfg.user = Some(db_user);
    cfg.password = Some(db_password);
    cfg.dbname = Some(db_name);

    let pool = cfg
        .create_pool(Some(Runtime::Tokio1), NoTls)
        .expect("Failed to create pool");

    init_db(&pool).await;

    let port: u16 = std::env::var("PORT")
        .unwrap_or_else(|_| "5001".to_string())
        .parse()
        .unwrap_or(5001);

    let app_state = Arc::new(AppState {
        pool,
        overview_cache: RwLock::new(None),
        recipe_cache: RwLock::new(HashMap::new()),
    });

    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(app_state.clone()))
            .app_data(web::JsonConfig::default().limit(1048576))
            .route("/recipes", web::get().to(get_recipes_overview))
            .route("/recipes/upload", web::post().to(upload_recipe))
            .route("/recipes/{recipeId}", web::get().to(get_recipe))
            .route("/recipes/{recipeId}/comments", web::post().to(add_comment))
            .route("/recipes/{recipeId}/ratings", web::post().to(add_rating))
    })
    .bind(("0.0.0.0", port))?
    .workers(num_cpus())
    .run()
    .await
}

fn num_cpus() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4)
}
