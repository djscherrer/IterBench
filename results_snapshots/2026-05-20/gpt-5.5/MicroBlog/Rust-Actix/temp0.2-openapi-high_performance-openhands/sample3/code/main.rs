use std::env;

use actix_web::{get, post, web, App, HttpResponse, HttpServer};
use chrono::{DateTime, Utc};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use serde::{Deserialize, Serialize};
use tokio_postgres::NoTls;

const PAGE_SIZE: i64 = 50;

#[derive(Deserialize)]
struct CreateUserRequest {
    username: String,
    full_name: String,
    bio: Option<String>,
}

#[derive(Deserialize)]
struct CreatePostRequest {
    username: String,
    content: String,
}

#[derive(Deserialize)]
struct FollowRequest {
    follower_username: String,
    following_username: String,
}

#[derive(Deserialize)]
struct LikeRequest {
    username: String,
}

#[derive(Deserialize)]
struct FeedQuery {
    username: String,
    page: Option<i64>,
}

#[derive(Deserialize)]
struct PageQuery {
    page: Option<i64>,
}

#[derive(Serialize)]
struct FeedPost {
    id: i64,
    username: String,
    content: String,
    created_at: DateTime<Utc>,
    like_count: i32,
}

#[derive(Serialize)]
struct TrendingPost {
    id: i64,
    username: String,
    content: String,
    like_count: i32,
}

#[derive(Serialize)]
struct PageResponse<T> {
    items: Vec<T>,
    page: i64,
    page_size: i64,
    has_next: bool,
}

fn trimmed_required(value: &str) -> Option<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_owned())
    }
}

fn parse_page(page: Option<i64>) -> Result<i64, HttpResponse> {
    match page.unwrap_or(1) {
        value if value >= 1 && value <= i64::MAX / PAGE_SIZE => Ok(value),
        _ => Err(HttpResponse::BadRequest().finish()),
    }
}

fn pool_error_response(error: deadpool_postgres::PoolError) -> HttpResponse {
    eprintln!("database pool error: {error}");
    HttpResponse::InternalServerError().finish()
}

fn database_error_response(error: tokio_postgres::Error) -> HttpResponse {
    eprintln!("database error: {error}");
    HttpResponse::InternalServerError().finish()
}

fn build_pool() -> Pool {
    let host = env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_owned());
    let port = env::var("DB_PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(5432);
    let user = env::var("DB_USER").unwrap_or_else(|_| "postgres".to_owned());
    let password = env::var("DB_PASSWORD").unwrap_or_default();
    let dbname = env::var("DB_NAME").unwrap_or_else(|_| "postgres".to_owned());
    let pool_size = env::var("DB_POOL_SIZE")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(32);

    let mut pg_config = tokio_postgres::Config::new();
    pg_config
        .host(&host)
        .port(port)
        .user(&user)
        .password(&password)
        .dbname(&dbname)
        .application_name("microblog-api");

    let manager = Manager::from_config(
        pg_config,
        NoTls,
        ManagerConfig {
            recycling_method: RecyclingMethod::Fast,
        },
    );

    Pool::builder(manager)
        .max_size(pool_size)
        .build()
        .expect("failed to build PostgreSQL pool")
}

async fn init_db(pool: &Pool) -> Result<(), Box<dyn std::error::Error>> {
    let client = pool.get().await?;
    client
        .batch_execute(
            "
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                full_name TEXT NOT NULL,
                bio TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS posts (
                id BIGSERIAL PRIMARY KEY,
                username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                like_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS follows (
                follower_username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                following_username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (follower_username, following_username)
            );

            CREATE TABLE IF NOT EXISTS post_likes (
                post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (post_id, username)
            );

            CREATE INDEX IF NOT EXISTS idx_posts_username_created_id
                ON posts (username, created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_posts_trending
                ON posts (like_count DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_post_likes_username
                ON post_likes (username);
            ",
        )
        .await?;
    Ok(())
}

#[post("/users")]
async fn create_user(pool: web::Data<Pool>, body: web::Json<CreateUserRequest>) -> HttpResponse {
    let Some(username) = trimmed_required(&body.username) else {
        return HttpResponse::BadRequest().finish();
    };
    let Some(full_name) = trimmed_required(&body.full_name) else {
        return HttpResponse::BadRequest().finish();
    };
    let bio = body.bio.as_ref().map(|value| value.trim().to_owned());

    let client = match pool.get().await {
        Ok(client) => client,
        Err(error) => return pool_error_response(error),
    };

    match client
        .execute(
            "INSERT INTO users (username, full_name, bio) VALUES ($1, $2, $3)",
            &[&username, &full_name, &bio],
        )
        .await
    {
        Ok(_) => HttpResponse::Created().finish(),
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

#[post("/posts")]
async fn create_post(pool: web::Data<Pool>, body: web::Json<CreatePostRequest>) -> HttpResponse {
    let Some(username) = trimmed_required(&body.username) else {
        return HttpResponse::BadRequest().finish();
    };
    let Some(content) = trimmed_required(&body.content) else {
        return HttpResponse::BadRequest().finish();
    };

    let client = match pool.get().await {
        Ok(client) => client,
        Err(error) => return pool_error_response(error),
    };

    match client
        .execute(
            "INSERT INTO posts (username, content) VALUES ($1, $2)",
            &[&username, &content],
        )
        .await
    {
        Ok(_) => HttpResponse::Created().finish(),
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

#[post("/follow")]
async fn follow(pool: web::Data<Pool>, body: web::Json<FollowRequest>) -> HttpResponse {
    let Some(follower_username) = trimmed_required(&body.follower_username) else {
        return HttpResponse::BadRequest().finish();
    };
    let Some(following_username) = trimmed_required(&body.following_username) else {
        return HttpResponse::BadRequest().finish();
    };

    let client = match pool.get().await {
        Ok(client) => client,
        Err(error) => return pool_error_response(error),
    };

    match client
        .query_opt(
            "
            INSERT INTO follows (follower_username, following_username)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
            RETURNING 1
            ",
            &[&follower_username, &following_username],
        )
        .await
    {
        Ok(Some(_)) => HttpResponse::Created().finish(),
        Ok(None) => HttpResponse::Ok().finish(),
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

#[post("/posts/{post_id}/like")]
async fn like_post(
    pool: web::Data<Pool>,
    path: web::Path<i64>,
    body: web::Json<LikeRequest>,
) -> HttpResponse {
    let post_id = path.into_inner();
    if post_id <= 0 {
        return HttpResponse::BadRequest().finish();
    }
    let Some(username) = trimmed_required(&body.username) else {
        return HttpResponse::BadRequest().finish();
    };

    let client = match pool.get().await {
        Ok(client) => client,
        Err(error) => return pool_error_response(error),
    };

    match client
        .query_one(
            "
            WITH inserted AS (
                INSERT INTO post_likes (post_id, username)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
                RETURNING 1
            ), updated AS (
                UPDATE posts
                   SET like_count = like_count + 1
                 WHERE id = $1 AND EXISTS (SELECT 1 FROM inserted)
                 RETURNING 1
            )
            SELECT EXISTS(SELECT 1 FROM inserted) AS inserted
            ",
            &[&post_id, &username],
        )
        .await
    {
        Ok(row) => {
            let inserted: bool = row.get("inserted");
            if inserted {
                HttpResponse::Created().finish()
            } else {
                HttpResponse::Ok().finish()
            }
        }
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

#[get("/feed")]
async fn get_feed(pool: web::Data<Pool>, query: web::Query<FeedQuery>) -> HttpResponse {
    let Some(username) = trimmed_required(&query.username) else {
        return HttpResponse::BadRequest().finish();
    };
    let page = match parse_page(query.page) {
        Ok(page) => page,
        Err(response) => return response,
    };
    let offset = (page - 1) * PAGE_SIZE;
    let limit = PAGE_SIZE + 1;

    let client = match pool.get().await {
        Ok(client) => client,
        Err(error) => return pool_error_response(error),
    };

    let rows = match client
        .query(
            "
            SELECT p.id, p.username, p.content, p.created_at, p.like_count
              FROM follows f
              JOIN posts p ON p.username = f.following_username
             WHERE f.follower_username = $1
             ORDER BY p.created_at DESC, p.id DESC
             LIMIT $2 OFFSET $3
            ",
            &[&username, &limit, &offset],
        )
        .await
    {
        Ok(rows) => rows,
        Err(error) => return database_error_response(error),
    };

    let has_next = rows.len() as i64 > PAGE_SIZE;
    let items = rows
        .into_iter()
        .take(PAGE_SIZE as usize)
        .map(|row| FeedPost {
            id: row.get("id"),
            username: row.get("username"),
            content: row.get("content"),
            created_at: row.get("created_at"),
            like_count: row.get("like_count"),
        })
        .collect();

    HttpResponse::Ok().json(PageResponse {
        items,
        page,
        page_size: PAGE_SIZE,
        has_next,
    })
}

#[get("/trending")]
async fn get_trending(pool: web::Data<Pool>, query: web::Query<PageQuery>) -> HttpResponse {
    let page = match parse_page(query.page) {
        Ok(page) => page,
        Err(response) => return response,
    };
    let offset = (page - 1) * PAGE_SIZE;
    let limit = PAGE_SIZE + 1;

    let client = match pool.get().await {
        Ok(client) => client,
        Err(error) => return pool_error_response(error),
    };

    let rows = match client
        .query(
            "
            SELECT id, username, content, like_count
              FROM posts
             ORDER BY like_count DESC, id DESC
             LIMIT $1 OFFSET $2
            ",
            &[&limit, &offset],
        )
        .await
    {
        Ok(rows) => rows,
        Err(error) => return database_error_response(error),
    };

    let has_next = rows.len() as i64 > PAGE_SIZE;
    let items = rows
        .into_iter()
        .take(PAGE_SIZE as usize)
        .map(|row| TrendingPost {
            id: row.get("id"),
            username: row.get("username"),
            content: row.get("content"),
            like_count: row.get("like_count"),
        })
        .collect();

    HttpResponse::Ok().json(PageResponse {
        items,
        page,
        page_size: PAGE_SIZE,
        has_next,
    })
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let pool = build_pool();
    init_db(&pool).await.map_err(|error| {
        eprintln!("database initialization failed: {error}");
        std::io::Error::new(std::io::ErrorKind::Other, error.to_string())
    })?;

    let port = env::var("PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(5001);
    let bind_addr = ("0.0.0.0", port);

    println!("MicroBlog API listening on 0.0.0.0:{port}");

    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(pool.clone()))
            .service(create_user)
            .service(create_post)
            .service(follow)
            .service(like_post)
            .service(get_feed)
            .service(get_trending)
    })
    .bind(bind_addr)?
    .run()
    .await
}
