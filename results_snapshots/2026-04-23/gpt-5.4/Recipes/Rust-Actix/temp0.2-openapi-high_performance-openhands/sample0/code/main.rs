use std::{env, fmt::Write as _, io};

use actix_web::{
    http::header::ContentType,
    web::{self, Data, Json, Path},
    App, HttpResponse, HttpServer,
};
use deadpool_postgres::{Config as PgConfig, ManagerConfig, Pool, RecyclingMethod, Runtime};
use serde::{Deserialize, Serialize};
use tokio_postgres::NoTls;
use uuid::Uuid;

const RECENT_LIMIT: i64 = 20;
const TOP_LIMIT: i64 = 20;
const JSON_LIMIT: usize = 64 * 1024;

const INIT_SQL: &str = r#"
CREATE TABLE IF NOT EXISTS recipes (
    id UUID PRIMARY KEY,
    title TEXT NOT NULL,
    ingredients TEXT[] NOT NULL,
    instructions TEXT NOT NULL,
    rating_sum BIGINT NOT NULL DEFAULT 0,
    rating_count BIGINT NOT NULL DEFAULT 0,
    avg_rating DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT recipes_title_not_blank CHECK (length(btrim(title)) > 0),
    CONSTRAINT recipes_instructions_not_blank CHECK (length(btrim(instructions)) > 0),
    CONSTRAINT recipes_has_ingredients CHECK (cardinality(ingredients) > 0)
);

CREATE TABLE IF NOT EXISTS recipe_comments (
    id BIGSERIAL PRIMARY KEY,
    recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    comment TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT recipe_comments_comment_not_blank CHECK (length(btrim(comment)) > 0)
);

CREATE TABLE IF NOT EXISTS recipe_ratings (
    id BIGSERIAL PRIMARY KEY,
    recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    rating SMALLINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT recipe_ratings_range CHECK (rating BETWEEN 1 AND 5)
);

CREATE INDEX IF NOT EXISTS idx_recipes_recent ON recipes (created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_recipes_top ON recipes (avg_rating DESC NULLS LAST, rating_count DESC, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_recipe_comments_lookup ON recipe_comments (recipe_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_recipe_ratings_lookup ON recipe_ratings (recipe_id);
"#;

const INSERT_RECIPE_SQL: &str = r#"
INSERT INTO recipes (id, title, ingredients, instructions)
VALUES ($1, $2, $3, $4)
"#;

const OVERVIEW_RECENT_SQL: &str = r#"
SELECT id, title
FROM recipes
ORDER BY created_at DESC, id DESC
LIMIT $1
"#;

const OVERVIEW_TOP_SQL: &str = r#"
SELECT id, title, avg_rating
FROM recipes
WHERE avg_rating IS NOT NULL
ORDER BY avg_rating DESC, rating_count DESC, created_at DESC, id DESC
LIMIT $1
"#;

const RECIPE_DETAILS_SQL: &str = r#"
SELECT
    r.id,
    r.title,
    r.ingredients,
    r.instructions,
    r.avg_rating,
    COALESCE(
        array_agg(rc.comment ORDER BY rc.created_at DESC, rc.id DESC)
        FILTER (WHERE rc.id IS NOT NULL),
        '{}'::TEXT[]
    ) AS comments
FROM recipes r
LEFT JOIN recipe_comments rc ON rc.recipe_id = r.id
WHERE r.id = $1
GROUP BY r.id, r.title, r.ingredients, r.instructions, r.avg_rating
"#;

const INSERT_COMMENT_SQL: &str = r#"
INSERT INTO recipe_comments (recipe_id, comment)
SELECT id, $2
FROM recipes
WHERE id = $1
"#;

const INSERT_RATING_SQL: &str = r#"
WITH updated_recipe AS (
    UPDATE recipes
    SET
        rating_sum = rating_sum + $2::BIGINT,
        rating_count = rating_count + 1,
        avg_rating = (rating_sum + $2::BIGINT)::DOUBLE PRECISION / (rating_count + 1)::DOUBLE PRECISION
    WHERE id = $1
    RETURNING id
)
INSERT INTO recipe_ratings (recipe_id, rating)
SELECT id, $2::SMALLINT
FROM updated_recipe
"#;

#[derive(Clone)]
struct AppState {
    pool: Pool,
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

#[derive(Deserialize)]
struct RecipePath {
    #[serde(rename = "recipeId")]
    recipe_id: String,
}

#[derive(Serialize)]
struct RecipeResponse {
    id: String,
    title: String,
    ingredients: Vec<String>,
    instructions: String,
    comments: Vec<CommentResponse>,
    #[serde(rename = "avgRating")]
    avg_rating: Option<f64>,
}

#[derive(Serialize)]
struct CommentResponse {
    comment: String,
}

#[derive(Clone)]
struct ValidatedRecipeInput {
    title: String,
    ingredients: Vec<String>,
    instructions: String,
}

fn invalid_input(message: &str) -> HttpResponse {
    HttpResponse::BadRequest().body(message.to_owned())
}

fn not_found() -> HttpResponse {
    HttpResponse::NotFound().finish()
}

fn internal_error(context: &str, error: impl std::fmt::Display) -> HttpResponse {
    eprintln!("{context}: {error}");
    HttpResponse::InternalServerError().finish()
}

fn parse_recipe_id(value: &str) -> Option<Uuid> {
    Uuid::parse_str(value).ok()
}

fn normalize_non_empty(value: &str) -> Option<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_owned())
    }
}

fn validate_recipe_input(payload: UploadRecipeRequest) -> Result<ValidatedRecipeInput, HttpResponse> {
    let title = normalize_non_empty(&payload.title)
        .ok_or_else(|| invalid_input("title must not be empty"))?;
    let instructions = normalize_non_empty(&payload.instructions)
        .ok_or_else(|| invalid_input("instructions must not be empty"))?;

    if payload.ingredients.is_empty() {
        return Err(invalid_input("ingredients must not be empty"));
    }

    let mut ingredients = Vec::with_capacity(payload.ingredients.len());
    for ingredient in payload.ingredients {
        let normalized = normalize_non_empty(&ingredient)
            .ok_or_else(|| invalid_input("ingredients must not contain empty values"))?;
        ingredients.push(normalized);
    }

    Ok(ValidatedRecipeInput {
        title,
        ingredients,
        instructions,
    })
}

fn escape_html(input: &str) -> String {
    let mut escaped = String::with_capacity(input.len());
    for ch in input.chars() {
        match ch {
            '&' => escaped.push_str("&amp;"),
            '<' => escaped.push_str("&lt;"),
            '>' => escaped.push_str("&gt;"),
            '"' => escaped.push_str("&quot;"),
            '\'' => escaped.push_str("&#39;"),
            _ => escaped.push(ch),
        }
    }
    escaped
}

fn render_overview_html(recent: &[(String, String)], top: &[(String, String, f64)]) -> String {
    let mut html = String::with_capacity(4096);
    html.push_str("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>Recipe Overview</title></head><body>");
    html.push_str("<h1>Recipe Sharing App</h1><section><h2>Recent Recipes</h2><ul>");

    if recent.is_empty() {
        html.push_str("<li>No recipes available.</li>");
    } else {
        for (id, title) in recent {
            let _ = write!(
                html,
                "<li><a href=\"/recipes/{id}\">{}</a></li>",
                escape_html(title)
            );
        }
    }

    html.push_str("</ul></section><section><h2>Top Rated Recipes</h2><ul>");

    if top.is_empty() {
        html.push_str("<li>No rated recipes available.</li>");
    } else {
        for (id, title, rating) in top {
            let _ = write!(
                html,
                "<li><a href=\"/recipes/{id}\">{}</a> - {:.2}/5</li>",
                escape_html(title),
                rating
            );
        }
    }

    html.push_str("</ul></section></body></html>");
    html
}

fn render_recipe_html(recipe: &RecipeResponse) -> String {
    let mut html = String::with_capacity(8192);
    html.push_str("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>");
    html.push_str(&escape_html(&recipe.title));
    html.push_str("</title></head><body>");
    let _ = write!(html, "<h1>{}</h1>", escape_html(&recipe.title));
    let _ = write!(html, "<p><strong>Recipe ID:</strong> {}</p>", recipe.id);

    match recipe.avg_rating {
        Some(avg_rating) => {
            let _ = write!(html, "<p><strong>Average rating:</strong> {:.2}/5</p>", avg_rating);
        }
        None => html.push_str("<p><strong>Average rating:</strong> Not rated yet</p>"),
    }

    html.push_str("<section><h2>Ingredients</h2><ul>");
    for ingredient in &recipe.ingredients {
        let _ = write!(html, "<li>{}</li>", escape_html(ingredient));
    }
    html.push_str("</ul></section>");

    let _ = write!(
        html,
        "<section><h2>Instructions</h2><p>{}</p></section>",
        escape_html(&recipe.instructions)
    );

    html.push_str("<section><h2>Comments</h2><ul>");
    if recipe.comments.is_empty() {
        html.push_str("<li>No comments yet.</li>");
    } else {
        for comment in &recipe.comments {
            let _ = write!(html, "<li>{}</li>", escape_html(&comment.comment));
        }
    }
    html.push_str("</ul></section></body></html>");
    html
}

async fn get_recipes_overview(state: Data<AppState>) -> HttpResponse {
    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(error) => return internal_error("failed to get database connection", error),
    };

    let recent_rows = match client.query(OVERVIEW_RECENT_SQL, &[&RECENT_LIMIT]).await {
        Ok(rows) => rows,
        Err(error) => return internal_error("failed to fetch recent recipes", error),
    };

    let top_rows = match client.query(OVERVIEW_TOP_SQL, &[&TOP_LIMIT]).await {
        Ok(rows) => rows,
        Err(error) => return internal_error("failed to fetch top recipes", error),
    };

    let recent = recent_rows
        .into_iter()
        .map(|row| {
            let id: Uuid = row.get(0);
            let title: String = row.get(1);
            (id.to_string(), title)
        })
        .collect::<Vec<_>>();

    let top = top_rows
        .into_iter()
        .map(|row| {
            let id: Uuid = row.get(0);
            let title: String = row.get(1);
            let avg_rating: f64 = row.get(2);
            (id.to_string(), title, avg_rating)
        })
        .collect::<Vec<_>>();

    HttpResponse::Ok()
        .content_type(ContentType::html())
        .body(render_overview_html(&recent, &top))
}

async fn upload_recipe(state: Data<AppState>, payload: Json<UploadRecipeRequest>) -> HttpResponse {
    let payload = match validate_recipe_input(payload.into_inner()) {
        Ok(payload) => payload,
        Err(response) => return response,
    };

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(error) => return internal_error("failed to get database connection", error),
    };

    let recipe_id = Uuid::new_v4();

    if let Err(error) = client
        .execute(
            INSERT_RECIPE_SQL,
            &[&recipe_id, &payload.title, &payload.ingredients, &payload.instructions],
        )
        .await
    {
        return internal_error("failed to insert recipe", error);
    }

    HttpResponse::Created().json(RecipeResponse {
        id: recipe_id.to_string(),
        title: payload.title,
        ingredients: payload.ingredients,
        instructions: payload.instructions,
        comments: Vec::new(),
        avg_rating: None,
    })
}

async fn get_recipe(state: Data<AppState>, path: Path<RecipePath>) -> HttpResponse {
    let recipe_id = match parse_recipe_id(&path.recipe_id) {
        Some(recipe_id) => recipe_id,
        None => return not_found(),
    };

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(error) => return internal_error("failed to get database connection", error),
    };

    let row = match client.query_opt(RECIPE_DETAILS_SQL, &[&recipe_id]).await {
        Ok(Some(row)) => row,
        Ok(None) => return not_found(),
        Err(error) => return internal_error("failed to fetch recipe details", error),
    };

    let recipe = RecipeResponse {
        id: row.get::<_, Uuid>(0).to_string(),
        title: row.get(1),
        ingredients: row.get(2),
        instructions: row.get(3),
        avg_rating: row.get(4),
        comments: row
            .get::<_, Vec<String>>(5)
            .into_iter()
            .map(|comment| CommentResponse { comment })
            .collect(),
    };

    HttpResponse::Ok()
        .content_type(ContentType::html())
        .body(render_recipe_html(&recipe))
}

async fn add_comment(state: Data<AppState>, path: Path<RecipePath>, payload: Json<CommentRequest>) -> HttpResponse {
    let recipe_id = match parse_recipe_id(&path.recipe_id) {
        Some(recipe_id) => recipe_id,
        None => return not_found(),
    };

    let comment = match normalize_non_empty(&payload.comment) {
        Some(comment) => comment,
        None => return invalid_input("comment must not be empty"),
    };

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(error) => return internal_error("failed to get database connection", error),
    };

    match client.execute(INSERT_COMMENT_SQL, &[&recipe_id, &comment]).await {
        Ok(0) => not_found(),
        Ok(_) => HttpResponse::Created().finish(),
        Err(error) => internal_error("failed to insert comment", error),
    }
}

async fn add_rating(state: Data<AppState>, path: Path<RecipePath>, payload: Json<RatingRequest>) -> HttpResponse {
    let recipe_id = match parse_recipe_id(&path.recipe_id) {
        Some(recipe_id) => recipe_id,
        None => return not_found(),
    };

    if !(1..=5).contains(&payload.rating) {
        return invalid_input("rating must be between 1 and 5");
    }

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(error) => return internal_error("failed to get database connection", error),
    };

    match client.execute(INSERT_RATING_SQL, &[&recipe_id, &payload.rating]).await {
        Ok(0) => not_found(),
        Ok(_) => HttpResponse::Created().finish(),
        Err(error) => internal_error("failed to insert rating", error),
    }
}

fn required_env(name: &str) -> io::Result<String> {
    env::var(name).map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, format!("missing environment variable {name}")))
}

fn parse_port(name: &str, default: u16) -> io::Result<u16> {
    match env::var(name) {
        Ok(value) => value.parse::<u16>().map_err(|error| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("invalid {name} value {value}: {error}"),
            )
        }),
        Err(_) => Ok(default),
    }
}

fn build_pool_from_env() -> io::Result<Pool> {
    let mut config = PgConfig::new();
    config.host = Some(required_env("DB_HOST")?);
    config.port = Some(parse_port("DB_PORT", 5432)?);
    config.user = Some(required_env("DB_USER")?);
    config.password = Some(required_env("DB_PASSWORD")?);
    config.dbname = Some(required_env("DB_NAME")?);
    config.manager = Some(ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    });

    config
        .create_pool(Some(Runtime::Tokio1), NoTls)
        .map_err(|error| io::Error::new(io::ErrorKind::Other, format!("failed to create database pool: {error}")))
}

async fn init_db(pool: &Pool) -> io::Result<()> {
    let client = pool
        .get()
        .await
        .map_err(|error| io::Error::new(io::ErrorKind::Other, format!("failed to get database connection: {error}")))?;

    client
        .batch_execute(INIT_SQL)
        .await
        .map_err(|error| io::Error::new(io::ErrorKind::Other, format!("failed to initialize database: {error}")))
}

#[actix_web::main]
async fn main() -> io::Result<()> {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();

    let port = parse_port("PORT", 5001)?;
    let pool = build_pool_from_env()?;
    init_db(&pool).await?;

    let state = Data::new(AppState { pool });
    let worker_count = std::thread::available_parallelism()
        .map(|parallelism| parallelism.get())
        .unwrap_or(4);

    println!("server listening on 0.0.0.0:{port}");

    HttpServer::new(move || {
        App::new()
            .app_data(state.clone())
            .app_data(web::JsonConfig::default().limit(JSON_LIMIT))
            .service(web::resource("/recipes").route(web::get().to(get_recipes_overview)))
            .service(web::resource("/recipes/upload").route(web::post().to(upload_recipe)))
            .service(web::resource("/recipes/{recipeId}").route(web::get().to(get_recipe)))
            .service(web::resource("/recipes/{recipeId}/comments").route(web::post().to(add_comment)))
            .service(web::resource("/recipes/{recipeId}/ratings").route(web::post().to(add_rating)))
    })
    .workers(worker_count)
    .bind(("0.0.0.0", port))?
    .run()
    .await
}
