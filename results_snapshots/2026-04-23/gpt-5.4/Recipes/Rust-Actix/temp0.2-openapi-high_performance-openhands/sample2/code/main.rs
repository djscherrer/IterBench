use std::{
    env, io,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};

use actix_web::{
    error::ErrorInternalServerError,
    middleware::Logger,
    web, App, HttpResponse, HttpServer, Result,
};
use deadpool_postgres::{Config as DbConfig, ManagerConfig, Pool, RecyclingMethod, Runtime};
use serde::{Deserialize, Serialize};
use tokio::sync::Mutex;
use tokio_postgres::{NoTls, Row};
use uuid::Uuid;

#[derive(Clone)]
struct AppState {
    pool: Pool,
    init_lock: Arc<Mutex<()>>,
    init_complete: Arc<AtomicBool>,
}

#[derive(Deserialize)]
struct UploadRecipeRequest {
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
    rating: i16,
}

#[derive(Serialize)]
struct CommentPayload {
    comment: String,
}

#[derive(Serialize)]
struct RecipePayload {
    id: String,
    title: String,
    ingredients: Vec<String>,
    instructions: String,
    comments: Vec<CommentPayload>,
    #[serde(rename = "avgRating")]
    avg_rating: Option<f64>,
}

struct RecipeLink {
    id: String,
    title: String,
}

fn required_env(name: &str) -> io::Result<String> {
    env::var(name).map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, format!("missing environment variable {name}")))
}

fn parse_recipe_id(recipe_id: &str) -> Option<Uuid> {
    Uuid::parse_str(recipe_id).ok()
}

fn validate_recipe(payload: &UploadRecipeRequest) -> bool {
    !payload.title.trim().is_empty()
        && !payload.instructions.trim().is_empty()
        && !payload.ingredients.is_empty()
        && payload.ingredients.iter().all(|item| !item.trim().is_empty())
}

fn validate_comment(payload: &CommentRequest) -> bool {
    !payload.comment.trim().is_empty()
}

fn validate_rating(payload: &RatingRequest) -> bool {
    (1..=5).contains(&payload.rating)
}

fn escape_html(input: &str) -> String {
    let mut output = String::with_capacity(input.len());
    for ch in input.chars() {
        match ch {
            '&' => output.push_str("&amp;"),
            '<' => output.push_str("&lt;"),
            '>' => output.push_str("&gt;"),
            '"' => output.push_str("&quot;"),
            '\'' => output.push_str("&#39;"),
            _ => output.push(ch),
        }
    }
    output
}

fn render_recipe_links(title: &str, recipes: &[RecipeLink]) -> String {
    let mut html = format!("<section><h2>{}</h2><ul>", escape_html(title));
    if recipes.is_empty() {
        html.push_str("<li>No recipes available.</li>");
    } else {
        for recipe in recipes {
            html.push_str(&format!(
                "<li><a href=\"/recipes/{}\">{}</a></li>",
                recipe.id,
                escape_html(&recipe.title)
            ));
        }
    }
    html.push_str("</ul></section>");
    html
}

fn recipe_link_from_row(row: &Row) -> RecipeLink {
    RecipeLink {
        id: row.get::<_, Uuid>("id").to_string(),
        title: row.get("title"),
    }
}

fn recipe_from_row(row: &Row, comments: Vec<CommentPayload>) -> RecipePayload {
    RecipePayload {
        id: row.get::<_, Uuid>("id").to_string(),
        title: row.get("title"),
        ingredients: row.get("ingredients"),
        instructions: row.get("instructions"),
        comments,
        avg_rating: row.get("avg_rating"),
    }
}

fn render_recipe_detail(recipe: &RecipePayload) -> String {
    let mut html = String::from("<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>");
    html.push_str(&escape_html(&recipe.title));
    html.push_str("</title></head><body>");
    html.push_str(&format!("<h1>{}</h1>", escape_html(&recipe.title)));
    html.push_str("<h2>Ingredients</h2><ul>");
    for ingredient in &recipe.ingredients {
        html.push_str(&format!("<li>{}</li>", escape_html(ingredient)));
    }
    html.push_str("</ul><h2>Instructions</h2><p>");
    html.push_str(&escape_html(&recipe.instructions).replace('\n', "<br/>"));
    html.push_str("</p><h2>Average Rating</h2><p>");
    match recipe.avg_rating {
        Some(value) => html.push_str(&format!("{value:.2}")),
        None => html.push_str("Not rated yet"),
    }
    html.push_str("</p><h2>Comments</h2><ul>");
    if recipe.comments.is_empty() {
        html.push_str("<li>No comments yet.</li>");
    } else {
        for comment in &recipe.comments {
            html.push_str(&format!("<li>{}</li>", escape_html(&comment.comment)));
        }
    }
    html.push_str("</ul></body></html>");
    html
}

fn build_pool() -> io::Result<Pool> {
    let mut config = DbConfig::new();
    config.host = Some(required_env("DB_HOST")?);
    config.port = Some(required_env("DB_PORT")?.parse().map_err(|error| {
        io::Error::new(io::ErrorKind::InvalidInput, format!("invalid DB_PORT: {error}"))
    })?);
    config.user = Some(required_env("DB_USER")?);
    config.password = Some(required_env("DB_PASSWORD")?);
    config.dbname = Some(required_env("DB_NAME")?);
    config.manager = Some(ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    });

    config
        .create_pool(Some(Runtime::Tokio1), NoTls)
        .map_err(|error| io::Error::new(io::ErrorKind::Other, format!("failed to create pool: {error}")))
}

async fn initialize_database(pool: &Pool) -> io::Result<()> {
    let client = pool
        .get()
        .await
        .map_err(|error| io::Error::new(io::ErrorKind::Other, format!("failed to get DB client: {error}")))?;

    client
        .batch_execute(
            "
            CREATE TABLE IF NOT EXISTS recipes (
                id UUID PRIMARY KEY,
                title TEXT NOT NULL,
                ingredients TEXT[] NOT NULL,
                instructions TEXT NOT NULL,
                avg_rating DOUBLE PRECISION NULL,
                ratings_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS recipe_comments (
                id BIGSERIAL PRIMARY KEY,
                recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                comment TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS recipe_ratings (
                id BIGSERIAL PRIMARY KEY,
                recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                rating SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes (created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_recipes_avg_rating ON recipes (avg_rating DESC NULLS LAST);
            CREATE INDEX IF NOT EXISTS idx_recipe_comments_recipe_id_created_at
                ON recipe_comments (recipe_id, created_at ASC);
            CREATE INDEX IF NOT EXISTS idx_recipe_ratings_recipe_id ON recipe_ratings (recipe_id);
            ",
        )
        .await
        .map_err(|error| io::Error::new(io::ErrorKind::Other, format!("failed to initialize schema: {error}")))?;

    Ok(())
}

async fn ensure_database_ready(state: &AppState) -> io::Result<()> {
    if state.init_complete.load(Ordering::Acquire) {
        return Ok(());
    }

    let _guard = state.init_lock.lock().await;
    if state.init_complete.load(Ordering::Acquire) {
        return Ok(());
    }

    initialize_database(&state.pool).await?;
    state.init_complete.store(true, Ordering::Release);
    Ok(())
}

async fn get_recipes_overview(state: web::Data<AppState>) -> Result<HttpResponse> {
    ensure_database_ready(state.get_ref())
        .await
        .map_err(ErrorInternalServerError)?;

    let client = state.pool.get().await.map_err(ErrorInternalServerError)?;

    let recent_rows = client
        .query(
            "SELECT id, title FROM recipes ORDER BY created_at DESC LIMIT 20",
            &[],
        )
        .await
        .map_err(ErrorInternalServerError)?;

    let top_rated_rows = client
        .query(
            "
            SELECT id, title
            FROM recipes
            WHERE avg_rating IS NOT NULL
            ORDER BY avg_rating DESC, ratings_count DESC, created_at DESC
            LIMIT 20
            ",
            &[],
        )
        .await
        .map_err(ErrorInternalServerError)?;

    let recent = recent_rows.iter().map(recipe_link_from_row).collect::<Vec<_>>();
    let top_rated = top_rated_rows
        .iter()
        .map(recipe_link_from_row)
        .collect::<Vec<_>>();

    let html = format!(
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Recipes</title></head><body><h1>Recipe Overview</h1>{}{}</body></html>",
        render_recipe_links("Recent Recipes", &recent),
        render_recipe_links("Top Rated Recipes", &top_rated),
    );

    Ok(HttpResponse::Ok()
        .content_type("text/html; charset=utf-8")
        .body(html))
}

async fn upload_recipe(
    state: web::Data<AppState>,
    payload: web::Json<UploadRecipeRequest>,
) -> Result<HttpResponse> {
    ensure_database_ready(state.get_ref())
        .await
        .map_err(ErrorInternalServerError)?;

    let payload = payload.into_inner();
    if !validate_recipe(&payload) {
        return Ok(HttpResponse::BadRequest().body("Invalid input"));
    }

    let client = state.pool.get().await.map_err(ErrorInternalServerError)?;
    let recipe_id = Uuid::new_v4();

    let row = client
        .query_one(
            "
            INSERT INTO recipes (id, title, ingredients, instructions)
            VALUES ($1, $2, $3, $4)
            RETURNING id, title, ingredients, instructions, avg_rating
            ",
            &[&recipe_id, &payload.title, &payload.ingredients, &payload.instructions],
        )
        .await
        .map_err(ErrorInternalServerError)?;

    let recipe = recipe_from_row(&row, Vec::new());

    Ok(HttpResponse::Created().json(recipe))
}

async fn get_recipe(state: web::Data<AppState>, recipe_id: web::Path<String>) -> Result<HttpResponse> {
    ensure_database_ready(state.get_ref())
        .await
        .map_err(ErrorInternalServerError)?;

    let Some(recipe_id) = parse_recipe_id(&recipe_id.into_inner()) else {
        return Ok(HttpResponse::NotFound().body("Recipe not found"));
    };

    let client = state.pool.get().await.map_err(ErrorInternalServerError)?;
    let recipe_row = client
        .query_opt(
            "
            SELECT id, title, ingredients, instructions, avg_rating
            FROM recipes
            WHERE id = $1
            ",
            &[&recipe_id],
        )
        .await
        .map_err(ErrorInternalServerError)?;

    let Some(recipe_row) = recipe_row else {
        return Ok(HttpResponse::NotFound().body("Recipe not found"));
    };

    let comment_rows = client
        .query(
            "
            SELECT comment
            FROM recipe_comments
            WHERE recipe_id = $1
            ORDER BY created_at ASC
            ",
            &[&recipe_id],
        )
        .await
        .map_err(ErrorInternalServerError)?;

    let comments = comment_rows
        .into_iter()
        .map(|row| CommentPayload { comment: row.get("comment") })
        .collect::<Vec<_>>();

    let recipe = recipe_from_row(&recipe_row, comments);
    let html = render_recipe_detail(&recipe);

    Ok(HttpResponse::Ok()
        .content_type("text/html; charset=utf-8")
        .body(html))
}

async fn add_comment(
    state: web::Data<AppState>,
    recipe_id: web::Path<String>,
    payload: web::Json<CommentRequest>,
) -> Result<HttpResponse> {
    ensure_database_ready(state.get_ref())
        .await
        .map_err(ErrorInternalServerError)?;

    let Some(recipe_id) = parse_recipe_id(&recipe_id.into_inner()) else {
        return Ok(HttpResponse::NotFound().body("Recipe not found"));
    };

    let payload = payload.into_inner();
    if !validate_comment(&payload) {
        return Ok(HttpResponse::BadRequest().body("Invalid input"));
    }

    let client = state.pool.get().await.map_err(ErrorInternalServerError)?;
    let inserted = client
        .query_opt(
            "
            INSERT INTO recipe_comments (recipe_id, comment)
            SELECT $1, $2
            WHERE EXISTS (SELECT 1 FROM recipes WHERE id = $1)
            RETURNING id
            ",
            &[&recipe_id, &payload.comment],
        )
        .await
        .map_err(ErrorInternalServerError)?;

    if inserted.is_none() {
        return Ok(HttpResponse::NotFound().body("Recipe not found"));
    }

    Ok(HttpResponse::Created().finish())
}

async fn add_rating(
    state: web::Data<AppState>,
    recipe_id: web::Path<String>,
    payload: web::Json<RatingRequest>,
) -> Result<HttpResponse> {
    ensure_database_ready(state.get_ref())
        .await
        .map_err(ErrorInternalServerError)?;

    let Some(recipe_id) = parse_recipe_id(&recipe_id.into_inner()) else {
        return Ok(HttpResponse::NotFound().body("Recipe not found"));
    };

    let payload = payload.into_inner();
    if !validate_rating(&payload) {
        return Ok(HttpResponse::BadRequest().body("Invalid input"));
    }

    let mut client = state.pool.get().await.map_err(ErrorInternalServerError)?;
    let transaction = client.transaction().await.map_err(ErrorInternalServerError)?;

    let recipe_state = transaction
        .query_opt(
            "SELECT avg_rating, ratings_count FROM recipes WHERE id = $1 FOR UPDATE",
            &[&recipe_id],
        )
        .await
        .map_err(ErrorInternalServerError)?;

    let Some(recipe_state) = recipe_state else {
        return Ok(HttpResponse::NotFound().body("Recipe not found"));
    };

    let current_avg: Option<f64> = recipe_state.get("avg_rating");
    let ratings_count: i32 = recipe_state.get("ratings_count");
    let new_avg = match current_avg {
        Some(avg) => ((avg * ratings_count as f64) + payload.rating as f64) / (ratings_count as f64 + 1.0),
        None => payload.rating as f64,
    };

    transaction
        .execute(
            "INSERT INTO recipe_ratings (recipe_id, rating) VALUES ($1, $2)",
            &[&recipe_id, &payload.rating],
        )
        .await
        .map_err(ErrorInternalServerError)?;

    transaction
        .execute(
            "
            UPDATE recipes
            SET avg_rating = $2,
                ratings_count = ratings_count + 1
            WHERE id = $1
            ",
            &[&recipe_id, &new_avg],
        )
        .await
        .map_err(ErrorInternalServerError)?;

    transaction.commit().await.map_err(ErrorInternalServerError)?;

    Ok(HttpResponse::Created().finish())
}

#[actix_web::main]
async fn main() -> io::Result<()> {
    env_logger::init();

    let pool = build_pool()?;
    let state = AppState {
        pool,
        init_lock: Arc::new(Mutex::new(())),
        init_complete: Arc::new(AtomicBool::new(false)),
    };

    let init_state = state.clone();
    tokio::spawn(async move {
        loop {
            match ensure_database_ready(&init_state).await {
                Ok(()) => break,
                Err(error) => {
                    eprintln!("database initialization retry failed: {error}");
                    tokio::time::sleep(Duration::from_secs(2)).await;
                }
            }
        }
    });

    let port = env::var("PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(5001);

    let state = web::Data::new(state);
    let workers = std::thread::available_parallelism()
        .map(|parallelism| parallelism.get())
        .unwrap_or(4);

    println!("Listening on 0.0.0.0:{port}");

    HttpServer::new(move || {
        App::new()
            .app_data(state.clone())
            .wrap(Logger::default())
            .route("/recipes", web::get().to(get_recipes_overview))
            .route("/recipes/upload", web::post().to(upload_recipe))
            .route("/recipes/{recipe_id}", web::get().to(get_recipe))
            .route("/recipes/{recipe_id}/comments", web::post().to(add_comment))
            .route("/recipes/{recipe_id}/ratings", web::post().to(add_rating))
    })
    .bind(("0.0.0.0", port))?
    .workers(workers)
    .run()
    .await
}
