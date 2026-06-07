use actix_web::{
    error::{ErrorBadRequest, ErrorInternalServerError},
    web::{self, Data, Json, Path},
    App, HttpResponse, HttpServer, Result,
};
use chrono::{DateTime, Utc};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use serde::{Deserialize, Serialize};
use std::{env, fmt::Write as _, io, thread};
use tokio_postgres::{NoTls, Row};
use uuid::Uuid;

#[derive(Clone)]
struct AppState {
    pool: Pool,
}

#[derive(Debug, Deserialize)]
struct UploadRecipeRequest {
    title: String,
    ingredients: Vec<String>,
    instructions: String,
}

#[derive(Debug, Deserialize)]
struct CreateCommentRequest {
    comment: String,
}

#[derive(Debug, Deserialize)]
struct CreateRatingRequest {
    rating: i16,
}

#[derive(Debug, Serialize)]
struct CommentResponse {
    comment: String,
}

#[derive(Debug, Serialize)]
struct RecipeResponse {
    id: String,
    title: String,
    ingredients: Vec<String>,
    instructions: String,
    comments: Vec<CommentResponse>,
    #[serde(rename = "avgRating")]
    avg_rating: Option<f64>,
}

#[derive(Debug)]
struct OverviewRecipe {
    id: Uuid,
    title: String,
    avg_rating: Option<f64>,
}

#[derive(Debug)]
struct RecipePage {
    id: Uuid,
    title: String,
    ingredients: Vec<String>,
    instructions: String,
    avg_rating: Option<f64>,
    rating_count: i32,
}

#[derive(Debug)]
struct RecipeComment {
    comment: String,
    created_at: DateTime<Utc>,
}

#[actix_web::main]
async fn main() -> io::Result<()> {
    env_logger::init();

    let port = env::var("PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(5001);

    let worker_count = thread::available_parallelism()
        .map(|count| count.get())
        .unwrap_or(4)
        .max(2);
    let pool_size = (worker_count * 8).clamp(16, 128);

    let pool = create_pool(pool_size).map_err(io_error)?;
    init_db(&pool).await.map_err(io_error)?;

    let state = Data::new(AppState { pool });

    HttpServer::new(move || {
        App::new()
            .app_data(state.clone())
            .route("/recipes", web::get().to(get_recipes_overview))
            .route("/recipes/upload", web::post().to(upload_recipe))
            .route("/recipes/{recipe_id}", web::get().to(get_recipe))
            .route(
                "/recipes/{recipe_id}/comments",
                web::post().to(add_comment),
            )
            .route(
                "/recipes/{recipe_id}/ratings",
                web::post().to(add_rating),
            )
    })
    .workers(worker_count)
    .bind(("0.0.0.0", port))?
    .run()
    .await
}

fn create_pool(pool_size: usize) -> std::result::Result<Pool, String> {
    let db_host = required_env("DB_HOST")?;
    let db_port = required_env("DB_PORT")?
        .parse::<u16>()
        .map_err(|error| format!("invalid DB_PORT: {error}"))?;
    let db_user = required_env("DB_USER")?;
    let db_password = required_env("DB_PASSWORD")?;
    let db_name = required_env("DB_NAME")?;

    let mut config = tokio_postgres::Config::new();
    config.host(&db_host);
    config.port(db_port);
    config.user(&db_user);
    config.password(&db_password);
    config.dbname(&db_name);

    let manager = Manager::from_config(
        config,
        NoTls,
        ManagerConfig {
            recycling_method: RecyclingMethod::Fast,
        },
    );

    Pool::builder(manager)
        .max_size(pool_size)
        .build()
        .map_err(|error| format!("failed to build database pool: {error}"))
}

async fn init_db(pool: &Pool) -> std::result::Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let client = pool.get().await?;
    client
        .batch_execute(
            "
            CREATE TABLE IF NOT EXISTS recipes (
                id UUID PRIMARY KEY,
                title TEXT NOT NULL,
                ingredients TEXT NOT NULL,
                instructions TEXT NOT NULL,
                avg_rating DOUBLE PRECISION,
                rating_count INTEGER NOT NULL DEFAULT 0,
                rating_total BIGINT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS comments (
                id BIGSERIAL PRIMARY KEY,
                recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                comment TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS ratings (
                id BIGSERIAL PRIMARY KEY,
                recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                rating SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_recipes_created_at
                ON recipes (created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_recipes_top_rated
                ON recipes (avg_rating DESC NULLS LAST, rating_count DESC, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_comments_recipe_created_at
                ON comments (recipe_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id
                ON ratings (recipe_id);
            ",
        )
        .await?;

    Ok(())
}

async fn get_recipes_overview(state: Data<AppState>) -> Result<HttpResponse> {
    let client = state.pool.get().await.map_err(internal_error)?;

    let recent_rows = client
        .query(
            "
            SELECT id, title, avg_rating
            FROM recipes
            ORDER BY created_at DESC
            LIMIT 20
            ",
            &[],
        )
        .await
        .map_err(internal_error)?;

    let top_rated_rows = client
        .query(
            "
            SELECT id, title, avg_rating
            FROM recipes
            WHERE avg_rating IS NOT NULL
            ORDER BY avg_rating DESC, rating_count DESC, created_at DESC
            LIMIT 20
            ",
            &[],
        )
        .await
        .map_err(internal_error)?;

    let recent = recent_rows
        .iter()
        .map(parse_overview_recipe)
        .collect::<std::result::Result<Vec<_>, _>>()
        .map_err(internal_error)?;
    let top_rated = top_rated_rows
        .iter()
        .map(parse_overview_recipe)
        .collect::<std::result::Result<Vec<_>, _>>()
        .map_err(internal_error)?;

    let html = render_overview_page(&recent, &top_rated);
    Ok(HttpResponse::Ok()
        .content_type("text/html; charset=utf-8")
        .body(html))
}

async fn upload_recipe(state: Data<AppState>, payload: Json<UploadRecipeRequest>) -> Result<HttpResponse> {
    let payload = payload.into_inner();
    validate_recipe_payload(&payload)?;

    let id = Uuid::new_v4();
    let ingredients = serde_json::to_string(&payload.ingredients).map_err(internal_error)?;

    let client = state.pool.get().await.map_err(internal_error)?;
    client
        .execute(
            "
            INSERT INTO recipes (id, title, ingredients, instructions)
            VALUES ($1, $2, $3, $4)
            ",
            &[&id, &payload.title, &ingredients, &payload.instructions],
        )
        .await
        .map_err(internal_error)?;

    let response = RecipeResponse {
        id: id.to_string(),
        title: payload.title,
        ingredients: payload.ingredients,
        instructions: payload.instructions,
        comments: Vec::new(),
        avg_rating: None,
    };

    Ok(HttpResponse::Created().json(response))
}

async fn get_recipe(state: Data<AppState>, recipe_id: Path<String>) -> Result<HttpResponse> {
    let recipe_id = match parse_recipe_id(recipe_id.into_inner()) {
        Some(value) => value,
        None => return Ok(HttpResponse::NotFound().finish()),
    };

    let client = state.pool.get().await.map_err(internal_error)?;
    let recipe_row = client
        .query_opt(
            "
            SELECT id, title, ingredients, instructions, avg_rating, rating_count
            FROM recipes
            WHERE id = $1
            ",
            &[&recipe_id],
        )
        .await
        .map_err(internal_error)?;

    let Some(recipe_row) = recipe_row else {
        return Ok(HttpResponse::NotFound().finish());
    };

    let recipe = parse_recipe_page(&recipe_row).map_err(internal_error)?;
    let comment_rows = client
        .query(
            "
            SELECT comment, created_at
            FROM comments
            WHERE recipe_id = $1
            ORDER BY created_at DESC
            ",
            &[&recipe_id],
        )
        .await
        .map_err(internal_error)?;

    let comments = comment_rows
        .iter()
        .map(parse_comment)
        .collect::<std::result::Result<Vec<_>, _>>()
        .map_err(internal_error)?;

    let html = render_recipe_page(&recipe, &comments);
    Ok(HttpResponse::Ok()
        .content_type("text/html; charset=utf-8")
        .body(html))
}

async fn add_comment(
    state: Data<AppState>,
    recipe_id: Path<String>,
    payload: Json<CreateCommentRequest>,
) -> Result<HttpResponse> {
    let recipe_id = match parse_recipe_id(recipe_id.into_inner()) {
        Some(value) => value,
        None => return Ok(HttpResponse::NotFound().finish()),
    };

    let payload = payload.into_inner();
    validate_comment_payload(&payload)?;

    let client = state.pool.get().await.map_err(internal_error)?;
    let inserted = client
        .execute(
            "
            INSERT INTO comments (recipe_id, comment)
            SELECT $1, $2
            FROM recipes
            WHERE id = $1
            ",
            &[&recipe_id, &payload.comment],
        )
        .await
        .map_err(internal_error)?;

    if inserted == 0 {
        return Ok(HttpResponse::NotFound().finish());
    }

    Ok(HttpResponse::Created().finish())
}

async fn add_rating(
    state: Data<AppState>,
    recipe_id: Path<String>,
    payload: Json<CreateRatingRequest>,
) -> Result<HttpResponse> {
    let recipe_id = match parse_recipe_id(recipe_id.into_inner()) {
        Some(value) => value,
        None => return Ok(HttpResponse::NotFound().finish()),
    };

    let payload = payload.into_inner();
    validate_rating_payload(&payload)?;

    let mut client = state.pool.get().await.map_err(internal_error)?;
    let transaction = client.transaction().await.map_err(internal_error)?;

    let rating_total_delta = i64::from(payload.rating);
    let updated = transaction
        .execute(
            "
            UPDATE recipes
            SET rating_count = rating_count + 1,
                rating_total = rating_total + $2,
                avg_rating = (rating_total + $2)::DOUBLE PRECISION / (rating_count + 1)
            WHERE id = $1
            ",
            &[&recipe_id, &rating_total_delta],
        )
        .await
        .map_err(internal_error)?;

    if updated == 0 {
        transaction.rollback().await.map_err(internal_error)?;
        return Ok(HttpResponse::NotFound().finish());
    }

    transaction
        .execute(
            "
            INSERT INTO ratings (recipe_id, rating)
            VALUES ($1, $2)
            ",
            &[&recipe_id, &payload.rating],
        )
        .await
        .map_err(internal_error)?;

    transaction.commit().await.map_err(internal_error)?;

    Ok(HttpResponse::Created().finish())
}

fn parse_overview_recipe(row: &Row) -> std::result::Result<OverviewRecipe, serde_json::Error> {
    Ok(OverviewRecipe {
        id: row.get("id"),
        title: row.get("title"),
        avg_rating: row.get("avg_rating"),
    })
}

fn parse_recipe_page(row: &Row) -> std::result::Result<RecipePage, serde_json::Error> {
    Ok(RecipePage {
        id: row.get("id"),
        title: row.get("title"),
        ingredients: serde_json::from_str(row.get::<_, &str>("ingredients"))?,
        instructions: row.get("instructions"),
        avg_rating: row.get("avg_rating"),
        rating_count: row.get("rating_count"),
    })
}

fn parse_comment(row: &Row) -> std::result::Result<RecipeComment, serde_json::Error> {
    Ok(RecipeComment {
        comment: row.get("comment"),
        created_at: row.get("created_at"),
    })
}

fn validate_recipe_payload(payload: &UploadRecipeRequest) -> Result<()> {
    if payload.title.trim().is_empty()
        || payload.instructions.trim().is_empty()
        || payload.ingredients.is_empty()
        || payload
            .ingredients
            .iter()
            .any(|ingredient| ingredient.trim().is_empty())
    {
        return Err(ErrorBadRequest("invalid input"));
    }

    Ok(())
}

fn validate_comment_payload(payload: &CreateCommentRequest) -> Result<()> {
    if payload.comment.trim().is_empty() {
        return Err(ErrorBadRequest("invalid input"));
    }

    Ok(())
}

fn validate_rating_payload(payload: &CreateRatingRequest) -> Result<()> {
    if !(1..=5).contains(&payload.rating) {
        return Err(ErrorBadRequest("invalid input"));
    }

    Ok(())
}

fn parse_recipe_id(value: String) -> Option<Uuid> {
    Uuid::parse_str(value.trim()).ok()
}

fn render_overview_page(recent: &[OverviewRecipe], top_rated: &[OverviewRecipe]) -> String {
    let mut html = String::with_capacity(4096);
    html.push_str("<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Recipes</title></head><body>");
    html.push_str("<h1>Recipe Overview</h1><h2>Recent Recipes</h2><ul>");

    if recent.is_empty() {
        html.push_str("<li>No recipes yet.</li>");
    } else {
        for recipe in recent {
            let _ = write!(
                html,
                "<li><a href=\"/recipes/{id}\">{title}</a>{rating}</li>",
                id = recipe.id,
                title = escape_html(&recipe.title),
                rating = recipe
                    .avg_rating
                    .map(|value| format!(" – avg rating {:.2}", value))
                    .unwrap_or_default()
            );
        }
    }

    html.push_str("</ul><h2>Top Rated Recipes</h2><ul>");

    if top_rated.is_empty() {
        html.push_str("<li>No rated recipes yet.</li>");
    } else {
        for recipe in top_rated {
            let _ = write!(
                html,
                "<li><a href=\"/recipes/{id}\">{title}</a>{rating}</li>",
                id = recipe.id,
                title = escape_html(&recipe.title),
                rating = recipe
                    .avg_rating
                    .map(|value| format!(" – avg rating {:.2}", value))
                    .unwrap_or_default()
            );
        }
    }

    html.push_str("</ul></body></html>");
    html
}

fn render_recipe_page(recipe: &RecipePage, comments: &[RecipeComment]) -> String {
    let mut html = String::with_capacity(8192);
    html.push_str("<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>");
    html.push_str(&escape_html(&recipe.title));
    html.push_str("</title></head><body>");
    let _ = write!(
        html,
        "<h1>{}</h1><p><strong>Recipe ID:</strong> {}</p>",
        escape_html(&recipe.title),
        recipe.id
    );
    match recipe.avg_rating {
        Some(avg_rating) => {
            let _ = write!(
                html,
                "<p><strong>Average rating:</strong> {:.2} ({} ratings)</p>",
                avg_rating,
                recipe.rating_count
            );
        }
        None => html.push_str("<p><strong>Average rating:</strong> Not rated yet</p>"),
    }
    html.push_str("<h2>Ingredients</h2><ul>");
    for ingredient in &recipe.ingredients {
        let _ = write!(html, "<li>{}</li>", escape_html(ingredient));
    }
    html.push_str("</ul><h2>Instructions</h2><p>");
    html.push_str(&escape_html(&recipe.instructions).replace('\n', "<br>"));
    html.push_str("</p><h2>Comments</h2><ul>");
    if comments.is_empty() {
        html.push_str("<li>No comments yet.</li>");
    } else {
        for comment in comments {
            let _ = write!(
                html,
                "<li><p>{}</p><small>{}</small></li>",
                escape_html(&comment.comment),
                comment.created_at.to_rfc3339()
            );
        }
    }
    html.push_str("</ul></body></html>");
    html
}

fn escape_html(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    for character in value.chars() {
        match character {
            '&' => escaped.push_str("&amp;"),
            '<' => escaped.push_str("&lt;"),
            '>' => escaped.push_str("&gt;"),
            '"' => escaped.push_str("&quot;"),
            '\'' => escaped.push_str("&#39;"),
            _ => escaped.push(character),
        }
    }
    escaped
}

fn required_env(name: &str) -> std::result::Result<String, String> {
    env::var(name).map_err(|_| format!("missing required environment variable {name}"))
}

fn internal_error<E: std::fmt::Display>(error: E) -> actix_web::Error {
    eprintln!("{error}");
    ErrorInternalServerError("internal server error")
}

fn io_error<E: std::fmt::Display>(error: E) -> io::Error {
    io::Error::new(io::ErrorKind::Other, error.to_string())
}
