use actix_web::middleware::Logger;
use actix_web::{web, App, HttpResponse, HttpServer, Responder};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use serde::{Deserialize, Serialize};
use std::env;
use tokio_postgres::NoTls;
use uuid::Uuid;

#[derive(Clone)]
struct AppState {
    pool: Pool,
}

#[derive(Serialize, Deserialize)]
struct UploadRecipeRequest {
    title: String,
    ingredients: Vec<String>,
    instructions: String,
}

#[derive(Serialize, Deserialize)]
struct CommentRequest {
    comment: String,
}

#[derive(Serialize, Deserialize)]
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
    #[serde(rename = "avgRating")]
    avg_rating: Option<f64>,
}

#[derive(Serialize)]
struct Comment {
    comment: String,
}

fn html_escape(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    for ch in input.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#39;"),
            _ => out.push(ch),
        }
    }
    out
}

async fn init_db(pool: &Pool) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let client = pool.get().await?;

    client
        .batch_execute(
            r#"
            CREATE TABLE IF NOT EXISTS recipes (
                id UUID PRIMARY KEY,
                title TEXT NOT NULL,
                ingredients TEXT[] NOT NULL,
                instructions TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS comments (
                id UUID PRIMARY KEY,
                recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                comment TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS ratings (
                id UUID PRIMARY KEY,
                recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_comments_recipe_id ON comments(recipe_id);
            CREATE INDEX IF NOT EXISTS idx_ratings_recipe_id ON ratings(recipe_id);
            CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes(created_at DESC);
            "#,
        )
        .await?;

    Ok(())
}

async fn get_recipes_overview(data: web::Data<AppState>) -> impl Responder {
    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => {
            return HttpResponse::InternalServerError().body("Server error");
        }
    };

    let rows = match client
        .query(
            r#"
            SELECT
                r.id,
                r.title,
                COALESCE(AVG(rt.rating)::float8, 0.0) AS avg_rating,
                r.created_at
            FROM recipes r
            LEFT JOIN ratings rt ON rt.recipe_id = r.id
            GROUP BY r.id, r.title, r.created_at
            ORDER BY avg_rating DESC, r.created_at DESC
            LIMIT 50
            "#,
            &[],
        )
        .await
    {
        Ok(rows) => rows,
        Err(_) => {
            return HttpResponse::InternalServerError().body("Server error");
        }
    };

    let mut body = String::from(
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Recipes</title></head><body>\
         <h1>Recipe Overview</h1><ul>",
    );

    for row in rows {
        let id: Uuid = row.get("id");
        let title: String = row.get("title");
        let avg_rating: f64 = row.get("avg_rating");
        body.push_str(&format!(
            "<li><a href=\"/recipes/{}\">{}</a> - Average Rating: {:.2}</li>",
            id,
            html_escape(&title),
            avg_rating
        ));
    }

    body.push_str("</ul></body></html>");

    HttpResponse::Ok().content_type("text/html").body(body)
}

async fn upload_recipe(
    data: web::Data<AppState>,
    payload: web::Json<UploadRecipeRequest>,
) -> impl Responder {
    if payload.title.trim().is_empty()
        || payload.instructions.trim().is_empty()
        || payload.ingredients.is_empty()
        || payload.ingredients.iter().any(|i| i.trim().is_empty())
    {
        return HttpResponse::BadRequest().body("Invalid input");
    }

    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let id = Uuid::new_v4();

    let insert_result = client
        .execute(
            r#"
            INSERT INTO recipes (id, title, ingredients, instructions)
            VALUES ($1, $2, $3, $4)
            "#,
            &[
                &id,
                &payload.title,
                &payload.ingredients,
                &payload.instructions,
            ],
        )
        .await;

    match insert_result {
        Ok(_) => {
            let recipe = Recipe {
                id: id.to_string(),
                title: payload.title.clone(),
                ingredients: payload.ingredients.clone(),
                instructions: payload.instructions.clone(),
                comments: Vec::new(),
                avg_rating: None,
            };

            HttpResponse::Created().json(recipe)
        }
        Err(_) => HttpResponse::BadRequest().body("Invalid input"),
    }
}

async fn get_recipe(data: web::Data<AppState>, recipe_id: web::Path<String>) -> impl Responder {
    let recipe_uuid = match Uuid::parse_str(&recipe_id.into_inner()) {
        Ok(id) => id,
        Err(_) => return HttpResponse::NotFound().body("Recipe not found"),
    };

    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let recipe_row = match client
        .query_opt(
            r#"
            SELECT
                r.id,
                r.title,
                r.ingredients,
                r.instructions,
                AVG(rt.rating)::float8 AS avg_rating
            FROM recipes r
            LEFT JOIN ratings rt ON rt.recipe_id = r.id
            WHERE r.id = $1
            GROUP BY r.id, r.title, r.ingredients, r.instructions
            "#,
            &[&recipe_uuid],
        )
        .await
    {
        Ok(row) => row,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let recipe_row = match recipe_row {
        Some(row) => row,
        None => return HttpResponse::NotFound().body("Recipe not found"),
    };

    let comments_rows = match client
        .query(
            r#"
            SELECT comment
            FROM comments
            WHERE recipe_id = $1
            ORDER BY created_at ASC
            "#,
            &[&recipe_uuid],
        )
        .await
    {
        Ok(rows) => rows,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let title: String = recipe_row.get("title");
    let ingredients: Vec<String> = recipe_row.get("ingredients");
    let instructions: String = recipe_row.get("instructions");
    let avg_rating: Option<f64> = recipe_row.get("avg_rating");

    let mut body = String::from(
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Recipe</title></head><body>",
    );

    body.push_str(&format!("<h1>{}</h1>", html_escape(&title)));
    body.push_str("<h2>Ingredients</h2><ul>");
    for ingredient in ingredients {
        body.push_str(&format!("<li>{}</li>", html_escape(&ingredient)));
    }
    body.push_str("</ul>");

    body.push_str("<h2>Instructions</h2>");
    body.push_str(&format!("<p>{}</p>", html_escape(&instructions)));

    body.push_str("<h2>Average Rating</h2>");
    match avg_rating {
        Some(r) => body.push_str(&format!("<p>{:.2}</p>", r)),
        None => body.push_str("<p>No ratings yet</p>"),
    }

    body.push_str("<h2>Comments</h2><ul>");
    for row in comments_rows {
        let comment: String = row.get("comment");
        body.push_str(&format!("<li>{}</li>", html_escape(&comment)));
    }
    body.push_str("</ul></body></html>");

    HttpResponse::Ok().content_type("text/html").body(body)
}

async fn add_comment(
    data: web::Data<AppState>,
    path: web::Path<String>,
    payload: web::Json<CommentRequest>,
) -> impl Responder {
    if payload.comment.trim().is_empty() {
        return HttpResponse::BadRequest().body("Invalid input");
    }

    let recipe_uuid = match Uuid::parse_str(&path.into_inner()) {
        Ok(id) => id,
        Err(_) => return HttpResponse::NotFound().body("Recipe not found"),
    };

    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let exists = match client
        .query_opt("SELECT id FROM recipes WHERE id = $1", &[&recipe_uuid])
        .await
    {
        Ok(v) => v.is_some(),
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    if !exists {
        return HttpResponse::NotFound().body("Recipe not found");
    }

    let comment_id = Uuid::new_v4();
    match client
        .execute(
            r#"
            INSERT INTO comments (id, recipe_id, comment)
            VALUES ($1, $2, $3)
            "#,
            &[&comment_id, &recipe_uuid, &payload.comment],
        )
        .await
    {
        Ok(_) => HttpResponse::Created().finish(),
        Err(_) => HttpResponse::BadRequest().body("Invalid input"),
    }
}

async fn add_rating(
    data: web::Data<AppState>,
    path: web::Path<String>,
    payload: web::Json<RatingRequest>,
) -> impl Responder {
    if payload.rating < 1 || payload.rating > 5 {
        return HttpResponse::BadRequest().body("Invalid input");
    }

    let recipe_uuid = match Uuid::parse_str(&path.into_inner()) {
        Ok(id) => id,
        Err(_) => return HttpResponse::NotFound().body("Recipe not found"),
    };

    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let exists = match client
        .query_opt("SELECT id FROM recipes WHERE id = $1", &[&recipe_uuid])
        .await
    {
        Ok(v) => v.is_some(),
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    if !exists {
        return HttpResponse::NotFound().body("Recipe not found");
    }

    let rating_id = Uuid::new_v4();
    match client
        .execute(
            r#"
            INSERT INTO ratings (id, recipe_id, rating)
            VALUES ($1, $2, $3)
            "#,
            &[&rating_id, &recipe_uuid, &payload.rating],
        )
        .await
    {
        Ok(_) => HttpResponse::Created().finish(),
        Err(_) => HttpResponse::BadRequest().body("Invalid input"),
    }
}

fn build_pg_config_from_env() -> tokio_postgres::Config {
    let mut cfg = tokio_postgres::Config::new();

    let host = env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string());
    let port = env::var("DB_PORT")
        .ok()
        .and_then(|v| v.parse::<u16>().ok())
        .unwrap_or(5432);
    let user = env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string());
    let password = env::var("DB_PASSWORD").unwrap_or_else(|_| "".to_string());
    let dbname = env::var("DB_NAME").unwrap_or_else(|_| "postgres".to_string());

    cfg.host(&host);
    cfg.port(port);
    cfg.user(&user);
    if !password.is_empty() {
        cfg.password(password);
    }
    cfg.dbname(&dbname);

    cfg
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let pg_config = build_pg_config_from_env();

    let mgr_config = ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    };
    let manager = Manager::from_config(pg_config, NoTls, mgr_config);
    let pool = Pool::builder(manager)
        .max_size(16)
        .build()
        .expect("failed to build pool");

    if let Err(err) = init_db(&pool).await {
        eprintln!("database initialization failed: {}", err);
        return Err(std::io::Error::new(
            std::io::ErrorKind::Other,
            "database initialization failed",
        ));
    }

    let state = AppState { pool };

    let port = env::var("PORT")
        .ok()
        .and_then(|v| v.parse::<u16>().ok())
        .unwrap_or(5001);

    HttpServer::new(move || {
        App::new()
            .wrap(Logger::default())
            .app_data(web::Data::new(state.clone()))
            .route("/recipes", web::get().to(get_recipes_overview))
            .route("/recipes/upload", web::post().to(upload_recipe))
            .route("/recipes/{recipeId}", web::get().to(get_recipe))
            .route(
                "/recipes/{recipeId}/comments",
                web::post().to(add_comment),
            )
            .route("/recipes/{recipeId}/ratings", web::post().to(add_rating))
    })
    .bind(("0.0.0.0", port))?
    .run()
    .await
}