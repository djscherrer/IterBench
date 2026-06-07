use actix_web::{http::header, web, App, HttpResponse, HttpServer};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use serde::{Deserialize, Serialize};
use std::{env, io};
use tokio_postgres::{Config as PgConfig, NoTls, Row};
use uuid::Uuid;

#[derive(Clone)]
struct AppState {
    pool: Pool,
}

#[derive(Debug, Deserialize)]
struct NewRecipe {
    title: String,
    ingredients: Vec<String>,
    instructions: String,
}

#[derive(Debug, Deserialize)]
struct NewComment {
    comment: String,
}

#[derive(Debug, Deserialize)]
struct NewRating {
    rating: i16,
}

#[derive(Debug, Serialize)]
struct CommentDto {
    comment: String,
}

#[derive(Debug, Serialize)]
struct RecipeDto {
    id: String,
    title: String,
    ingredients: Vec<String>,
    instructions: String,
    comments: Vec<CommentDto>,
    #[serde(rename = "avgRating")]
    avg_rating: Option<f64>,
}

#[derive(Debug)]
struct RecipePage {
    id: Uuid,
    title: String,
    ingredients: Vec<String>,
    instructions: String,
    avg_rating: Option<f64>,
    comments: Vec<String>,
}

#[derive(Debug)]
struct RecipeSummary {
    id: Uuid,
    title: String,
    avg_rating: Option<f64>,
}

fn env_var(name: &str, default: &str) -> String {
    env::var(name).unwrap_or_else(|_| default.to_owned())
}

fn build_pool() -> io::Result<Pool> {
    let mut cfg = PgConfig::new();
    cfg.host(&env_var("DB_HOST", "localhost"));
    cfg.port(env_var("DB_PORT", "5432").parse::<u16>().map_err(|err| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("invalid DB_PORT: {err}"),
        )
    })?);
    cfg.user(&env_var("DB_USER", "postgres"));
    cfg.password(&env_var("DB_PASSWORD", "postgres"));
    cfg.dbname(&env_var("DB_NAME", "postgres"));

    let manager = Manager::from_config(
        cfg,
        NoTls,
        ManagerConfig {
            recycling_method: RecyclingMethod::Fast,
        },
    );

    let pool_size = env::var("DB_POOL_SIZE")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|size| *size > 0)
        .unwrap_or(32);

    Pool::builder(manager)
        .max_size(pool_size)
        .build()
        .map_err(|err| io::Error::new(io::ErrorKind::Other, format!("pool build failed: {err}")))
}

async fn init_db(pool: &Pool) -> io::Result<()> {
    let client = pool.get().await.map_err(|err| {
        io::Error::new(io::ErrorKind::Other, format!("database pool error: {err}"))
    })?;

    client
        .batch_execute(
            "
            CREATE TABLE IF NOT EXISTS recipes (
                id UUID PRIMARY KEY,
                title TEXT NOT NULL,
                ingredients TEXT[] NOT NULL,
                instructions TEXT NOT NULL,
                rating_sum BIGINT NOT NULL DEFAULT 0,
                rating_count BIGINT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            ALTER TABLE recipes ADD COLUMN IF NOT EXISTS rating_sum BIGINT NOT NULL DEFAULT 0;
            ALTER TABLE recipes ADD COLUMN IF NOT EXISTS rating_count BIGINT NOT NULL DEFAULT 0;

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

            CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes (created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_recipes_rating_sort ON recipes (
                (CASE WHEN rating_count > 0 THEN rating_sum::double precision / rating_count::double precision ELSE NULL END) DESC NULLS LAST,
                rating_count DESC,
                created_at DESC
            );
            CREATE INDEX IF NOT EXISTS idx_comments_recipe_created_at ON comments (recipe_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id ON ratings (recipe_id);
            CREATE INDEX IF NOT EXISTS idx_ratings_recipe_rating ON ratings (recipe_id, rating);

            UPDATE recipes r
            SET rating_sum = totals.rating_sum, rating_count = totals.rating_count
            FROM (
                SELECT r2.id, COALESCE(SUM(rt.rating)::BIGINT, 0) AS rating_sum, COUNT(rt.id)::BIGINT AS rating_count
                FROM recipes r2
                LEFT JOIN ratings rt ON rt.recipe_id = r2.id
                GROUP BY r2.id
            ) totals
            WHERE r.id = totals.id;
            ",
        )
        .await
        .map_err(|err| io::Error::new(io::ErrorKind::Other, format!("database init failed: {err}")))
}

fn html_escape(input: &str) -> String {
    let mut escaped = String::with_capacity(input.len());
    for ch in input.chars() {
        match ch {
            '&' => escaped.push_str("&amp;"),
            '<' => escaped.push_str("&lt;"),
            '>' => escaped.push_str("&gt;"),
            '"' => escaped.push_str("&quot;"),
            '\'' => escaped.push_str("&#x27;"),
            _ => escaped.push(ch),
        }
    }
    escaped
}

fn html_response(body: String) -> HttpResponse {
    HttpResponse::Ok()
        .insert_header((header::CONTENT_TYPE, "text/html; charset=utf-8"))
        .body(body)
}

fn bad_request(message: &str) -> HttpResponse {
    HttpResponse::BadRequest().body(message.to_owned())
}

fn internal_error() -> HttpResponse {
    HttpResponse::InternalServerError().finish()
}

fn not_found() -> HttpResponse {
    HttpResponse::NotFound().finish()
}

fn validate_recipe(input: NewRecipe) -> Result<NewRecipe, HttpResponse> {
    let title = input.title.trim().to_owned();
    let instructions = input.instructions.trim().to_owned();
    let ingredients = input
        .ingredients
        .into_iter()
        .map(|ingredient| ingredient.trim().to_owned())
        .filter(|ingredient| !ingredient.is_empty())
        .collect::<Vec<_>>();

    if title.is_empty() || instructions.is_empty() || ingredients.is_empty() {
        return Err(bad_request(
            "title, instructions, and at least one ingredient are required",
        ));
    }

    Ok(NewRecipe {
        title,
        ingredients,
        instructions,
    })
}

fn row_to_recipe(row: Row) -> RecipeDto {
    let id: Uuid = row.get("id");
    RecipeDto {
        id: id.to_string(),
        title: row.get("title"),
        ingredients: row.get("ingredients"),
        instructions: row.get("instructions"),
        comments: Vec::new(),
        avg_rating: None,
    }
}

async fn recipes_overview(state: web::Data<AppState>) -> HttpResponse {
    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(_) => return internal_error(),
    };

    let recent_rows = match client
        .query(
            "
            SELECT id, title,
                   CASE WHEN rating_count > 0 THEN rating_sum::float8 / rating_count ELSE NULL END AS avg_rating
            FROM recipes
            ORDER BY created_at DESC
            LIMIT 50
            ",
            &[],
        )
        .await
    {
        Ok(rows) => rows,
        Err(_) => return internal_error(),
    };

    let top_rows = match client
        .query(
            "
            SELECT id, title,
                   CASE WHEN rating_count > 0 THEN rating_sum::float8 / rating_count ELSE NULL END AS avg_rating,
                   rating_count
            FROM recipes
            ORDER BY avg_rating DESC NULLS LAST, rating_count DESC, created_at DESC
            LIMIT 50
            ",
            &[],
        )
        .await
    {
        Ok(rows) => rows,
        Err(_) => return internal_error(),
    };

    let recent = recent_rows
        .into_iter()
        .map(|row| RecipeSummary {
            id: row.get("id"),
            title: row.get("title"),
            avg_rating: row.get("avg_rating"),
        })
        .collect::<Vec<_>>();
    let top = top_rows
        .into_iter()
        .map(|row| RecipeSummary {
            id: row.get("id"),
            title: row.get("title"),
            avg_rating: row.get("avg_rating"),
        })
        .collect::<Vec<_>>();

    let mut body = String::from(
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>Recipes</title></head><body><h1>Recipes</h1>",
    );
    append_summary_section(&mut body, "Recent recipes", &recent);
    append_summary_section(&mut body, "Top-rated recipes", &top);
    body.push_str("</body></html>");
    html_response(body)
}

fn append_summary_section(body: &mut String, title: &str, recipes: &[RecipeSummary]) {
    body.push_str("<section><h2>");
    body.push_str(&html_escape(title));
    body.push_str("</h2><ul>");
    for recipe in recipes {
        body.push_str("<li><a href=\"/recipes/");
        body.push_str(&recipe.id.to_string());
        body.push_str("\">");
        body.push_str(&html_escape(&recipe.title));
        body.push_str("</a>");
        if let Some(avg) = recipe.avg_rating {
            body.push_str(" - Rating: ");
            body.push_str(&format!("{avg:.2}"));
        }
        body.push_str("</li>");
    }
    body.push_str("</ul></section>");
}

async fn upload_recipe(state: web::Data<AppState>, payload: web::Json<NewRecipe>) -> HttpResponse {
    let recipe = match validate_recipe(payload.into_inner()) {
        Ok(recipe) => recipe,
        Err(response) => return response,
    };

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(_) => return internal_error(),
    };

    let id = Uuid::new_v4();
    match client
        .query_one(
            "
            INSERT INTO recipes (id, title, ingredients, instructions)
            VALUES ($1, $2, $3, $4)
            RETURNING id, title, ingredients, instructions
            ",
            &[
                &id,
                &recipe.title,
                &recipe.ingredients,
                &recipe.instructions,
            ],
        )
        .await
    {
        Ok(row) => HttpResponse::Created().json(row_to_recipe(row)),
        Err(_) => internal_error(),
    }
}

async fn get_recipe(state: web::Data<AppState>, recipe_id: web::Path<String>) -> HttpResponse {
    let recipe_id = match Uuid::parse_str(recipe_id.as_str()) {
        Ok(id) => id,
        Err(_) => return not_found(),
    };

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(_) => return internal_error(),
    };

    let row = match client
        .query_opt(
            "
            SELECT id, title, ingredients, instructions,
                   CASE WHEN rating_count > 0 THEN rating_sum::float8 / rating_count ELSE NULL END AS avg_rating
            FROM recipes
            WHERE id = $1
            ",
            &[&recipe_id],
        )
        .await
    {
        Ok(Some(row)) => row,
        Ok(None) => return not_found(),
        Err(_) => return internal_error(),
    };

    let comments = match client
        .query(
            "SELECT comment FROM comments WHERE recipe_id = $1 ORDER BY created_at DESC LIMIT 200",
            &[&recipe_id],
        )
        .await
    {
        Ok(rows) => rows
            .into_iter()
            .map(|row| row.get::<_, String>("comment"))
            .collect::<Vec<_>>(),
        Err(_) => return internal_error(),
    };

    let recipe = RecipePage {
        id: row.get("id"),
        title: row.get("title"),
        ingredients: row.get("ingredients"),
        instructions: row.get("instructions"),
        avg_rating: row.get("avg_rating"),
        comments,
    };

    html_response(render_recipe_page(&recipe))
}

fn render_recipe_page(recipe: &RecipePage) -> String {
    let mut body = String::from("<!doctype html><html><head><meta charset=\"utf-8\"><title>");
    body.push_str(&html_escape(&recipe.title));
    body.push_str("</title></head><body><article><h1>");
    body.push_str(&html_escape(&recipe.title));
    body.push_str("</h1><p>ID: ");
    body.push_str(&recipe.id.to_string());
    body.push_str("</p><p>Average rating: ");
    match recipe.avg_rating {
        Some(avg) => body.push_str(&format!("{avg:.2}")),
        None => body.push_str("Not rated"),
    }
    body.push_str("</p><h2>Ingredients</h2><ul>");
    for ingredient in &recipe.ingredients {
        body.push_str("<li>");
        body.push_str(&html_escape(ingredient));
        body.push_str("</li>");
    }
    body.push_str("</ul><h2>Instructions</h2><p>");
    body.push_str(&html_escape(&recipe.instructions));
    body.push_str("</p><h2>Comments</h2><ul>");
    for comment in &recipe.comments {
        body.push_str("<li>");
        body.push_str(&html_escape(comment));
        body.push_str("</li>");
    }
    body.push_str("</ul></article></body></html>");
    body
}

async fn add_comment(
    state: web::Data<AppState>,
    recipe_id: web::Path<String>,
    payload: web::Json<NewComment>,
) -> HttpResponse {
    let recipe_id = match Uuid::parse_str(recipe_id.as_str()) {
        Ok(id) => id,
        Err(_) => return not_found(),
    };
    let comment = payload.comment.trim().to_owned();
    if comment.is_empty() {
        return bad_request("comment is required");
    }

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(_) => return internal_error(),
    };

    match client
        .query_opt(
            "
            INSERT INTO comments (recipe_id, comment)
            SELECT id, $2 FROM recipes WHERE id = $1
            RETURNING id
            ",
            &[&recipe_id, &comment],
        )
        .await
    {
        Ok(Some(_)) => HttpResponse::Created().finish(),
        Ok(None) => not_found(),
        Err(_) => internal_error(),
    }
}

async fn add_rating(
    state: web::Data<AppState>,
    recipe_id: web::Path<String>,
    payload: web::Json<NewRating>,
) -> HttpResponse {
    let recipe_id = match Uuid::parse_str(recipe_id.as_str()) {
        Ok(id) => id,
        Err(_) => return not_found(),
    };
    if !(1..=5).contains(&payload.rating) {
        return bad_request("rating must be between 1 and 5");
    }

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(_) => return internal_error(),
    };

    match client
        .query_opt(
            "
            WITH target AS (
                SELECT id FROM recipes WHERE id = $1
            ), inserted AS (
                INSERT INTO ratings (recipe_id, rating)
                SELECT id, $2::smallint FROM target
                RETURNING rating
            )
            UPDATE recipes
            SET rating_sum = rating_sum + $2::bigint,
                rating_count = rating_count + 1
            WHERE id = (SELECT id FROM target)
              AND EXISTS (SELECT 1 FROM inserted)
            RETURNING id
            ",
            &[&recipe_id, &payload.rating],
        )
        .await
    {
        Ok(Some(_)) => HttpResponse::Created().finish(),
        Ok(None) => not_found(),
        Err(_) => internal_error(),
    }
}

#[actix_web::main]
async fn main() -> io::Result<()> {
    env_logger::init();

    let pool = build_pool()?;
    init_db(&pool).await?;

    let port = env::var("PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(5001);
    let bind_addr = ("0.0.0.0", port);
    let state = web::Data::new(AppState { pool });

    HttpServer::new(move || {
        App::new()
            .app_data(state.clone())
            .route("/recipes", web::get().to(recipes_overview))
            .route("/recipes/upload", web::post().to(upload_recipe))
            .route("/recipes/{recipeId}", web::get().to(get_recipe))
            .route("/recipes/{recipeId}/comments", web::post().to(add_comment))
            .route("/recipes/{recipeId}/ratings", web::post().to(add_rating))
    })
    .bind(bind_addr)?
    .run()
    .await
}
