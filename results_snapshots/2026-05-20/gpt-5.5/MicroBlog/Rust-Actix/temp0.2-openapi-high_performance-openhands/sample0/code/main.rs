use actix_web::http::StatusCode;
use actix_web::{middleware::Logger, web, App, HttpResponse, HttpServer, ResponseError};
use chrono::{DateTime, Utc};
use deadpool_postgres::{Config, ManagerConfig, Pool, PoolConfig, RecyclingMethod, Runtime};
use serde::{Deserialize, Serialize};
use std::env;
use std::fmt;
use std::io;
use tokio_postgres::NoTls;

const PAGE_SIZE: i64 = 50;

#[derive(Clone)]
struct AppState {
    pool: Pool,
}

#[derive(Debug)]
enum ApiError {
    BadRequest,
    Database,
}

impl fmt::Display for ApiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ApiError::BadRequest => f.write_str("invalid input"),
            ApiError::Database => f.write_str("database error"),
        }
    }
}

impl ResponseError for ApiError {
    fn status_code(&self) -> StatusCode {
        match self {
            ApiError::BadRequest => StatusCode::BAD_REQUEST,
            ApiError::Database => StatusCode::INTERNAL_SERVER_ERROR,
        }
    }

    fn error_response(&self) -> HttpResponse {
        HttpResponse::build(self.status_code()).finish()
    }
}

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
struct PageQuery {
    page: Option<i64>,
}

#[derive(Deserialize)]
struct FeedQuery {
    username: String,
    page: Option<i64>,
}

#[derive(Serialize)]
struct FeedPost {
    id: i64,
    username: String,
    content: String,
    created_at: DateTime<Utc>,
    like_count: i64,
}

#[derive(Serialize)]
struct TrendingPost {
    id: i64,
    username: String,
    content: String,
    like_count: i64,
}

#[derive(Serialize)]
struct PaginatedResponse<T> {
    items: Vec<T>,
    page: i64,
    page_size: i64,
    has_next: bool,
}

fn valid_required(value: &str) -> bool {
    !value.trim().is_empty()
}

fn normalize_page(page: Option<i64>) -> Result<i64, ApiError> {
    let page = page.unwrap_or(1);
    if page < 1 {
        return Err(ApiError::BadRequest);
    }
    page.checked_sub(1)
        .and_then(|p| p.checked_mul(PAGE_SIZE))
        .map(|_| page)
        .ok_or(ApiError::BadRequest)
}

fn page_offset(page: i64) -> Result<i64, ApiError> {
    page.checked_sub(1)
        .and_then(|p| p.checked_mul(PAGE_SIZE))
        .ok_or(ApiError::BadRequest)
}

fn client_error(err: tokio_postgres::Error) -> ApiError {
    if let Some(db_err) = err.as_db_error() {
        match db_err.code().code() {
            "23505" | "23503" | "23502" | "22P02" | "23514" => ApiError::BadRequest,
            _ => ApiError::Database,
        }
    } else {
        ApiError::Database
    }
}

async fn create_user(
    state: web::Data<AppState>,
    body: web::Json<CreateUserRequest>,
) -> Result<HttpResponse, ApiError> {
    if !valid_required(&body.username) || !valid_required(&body.full_name) {
        return Err(ApiError::BadRequest);
    }

    let client = state.pool.get().await.map_err(|_| ApiError::Database)?;
    client
        .execute(
            "INSERT INTO users (username, full_name, bio) VALUES ($1, $2, $3)",
            &[&body.username, &body.full_name, &body.bio],
        )
        .await
        .map_err(client_error)?;

    Ok(HttpResponse::Created().finish())
}

async fn create_post(
    state: web::Data<AppState>,
    body: web::Json<CreatePostRequest>,
) -> Result<HttpResponse, ApiError> {
    if !valid_required(&body.username) || !valid_required(&body.content) {
        return Err(ApiError::BadRequest);
    }

    let client = state.pool.get().await.map_err(|_| ApiError::Database)?;
    client
        .execute(
            "INSERT INTO posts (username, content) VALUES ($1, $2)",
            &[&body.username, &body.content],
        )
        .await
        .map_err(client_error)?;

    Ok(HttpResponse::Created().finish())
}

async fn follow_user(
    state: web::Data<AppState>,
    body: web::Json<FollowRequest>,
) -> Result<HttpResponse, ApiError> {
    if !valid_required(&body.follower_username) || !valid_required(&body.following_username) {
        return Err(ApiError::BadRequest);
    }

    let client = state.pool.get().await.map_err(|_| ApiError::Database)?;
    let inserted = client
        .query_opt(
            "INSERT INTO follows (follower_username, following_username)
             VALUES ($1, $2)
             ON CONFLICT DO NOTHING
             RETURNING 1",
            &[&body.follower_username, &body.following_username],
        )
        .await
        .map_err(client_error)?
        .is_some();

    if inserted {
        Ok(HttpResponse::Created().finish())
    } else {
        Ok(HttpResponse::Ok().finish())
    }
}

async fn like_post(
    state: web::Data<AppState>,
    post_id: web::Path<i64>,
    body: web::Json<LikeRequest>,
) -> Result<HttpResponse, ApiError> {
    let post_id = post_id.into_inner();
    if post_id < 1 || !valid_required(&body.username) {
        return Err(ApiError::BadRequest);
    }

    let client = state.pool.get().await.map_err(|_| ApiError::Database)?;
    let row = client
        .query_one(
            "WITH inserted AS (
                 INSERT INTO likes (post_id, username)
                 VALUES ($1, $2)
                 ON CONFLICT DO NOTHING
                 RETURNING 1
             ), updated AS (
                 UPDATE posts
                 SET like_count = like_count + 1
                 WHERE id = $1 AND EXISTS (SELECT 1 FROM inserted)
                 RETURNING id
             )
             SELECT EXISTS (SELECT 1 FROM inserted) AS inserted",
            &[&post_id, &body.username],
        )
        .await
        .map_err(client_error)?;

    let inserted: bool = row.get("inserted");
    if inserted {
        Ok(HttpResponse::Created().finish())
    } else {
        Ok(HttpResponse::Ok().finish())
    }
}

async fn get_feed(
    state: web::Data<AppState>,
    query: web::Query<FeedQuery>,
) -> Result<HttpResponse, ApiError> {
    if !valid_required(&query.username) {
        return Err(ApiError::BadRequest);
    }
    let page = normalize_page(query.page)?;
    let offset = page_offset(page)?;
    let limit = PAGE_SIZE + 1;

    let client = state.pool.get().await.map_err(|_| ApiError::Database)?;
    let rows = client
        .query(
            "SELECT p.id, p.username, p.content, p.created_at, p.like_count
             FROM follows f
             JOIN posts p ON p.username = f.following_username
             WHERE f.follower_username = $1
             ORDER BY p.created_at DESC, p.id DESC
             LIMIT $2 OFFSET $3",
            &[&query.username, &limit, &offset],
        )
        .await
        .map_err(client_error)?;

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

    Ok(HttpResponse::Ok().json(PaginatedResponse {
        items,
        page,
        page_size: PAGE_SIZE,
        has_next,
    }))
}

async fn get_trending(
    state: web::Data<AppState>,
    query: web::Query<PageQuery>,
) -> Result<HttpResponse, ApiError> {
    let page = normalize_page(query.page)?;
    let offset = page_offset(page)?;
    let limit = PAGE_SIZE + 1;

    let client = state.pool.get().await.map_err(|_| ApiError::Database)?;
    let rows = client
        .query(
            "SELECT id, username, content, like_count
             FROM posts
             ORDER BY like_count DESC, id DESC
             LIMIT $1 OFFSET $2",
            &[&limit, &offset],
        )
        .await
        .map_err(client_error)?;

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

    Ok(HttpResponse::Ok().json(PaginatedResponse {
        items,
        page,
        page_size: PAGE_SIZE,
        has_next,
    }))
}

async fn init_db(pool: &Pool) -> io::Result<()> {
    let client = pool.get().await.map_err(io_other)?;

    client
        .batch_execute(
            "CREATE TABLE IF NOT EXISTS users (
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
                 like_count BIGINT NOT NULL DEFAULT 0
             );

             CREATE TABLE IF NOT EXISTS follows (
                 follower_username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                 following_username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                 PRIMARY KEY (follower_username, following_username)
             );

             CREATE TABLE IF NOT EXISTS likes (
                 post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                 username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                 PRIMARY KEY (post_id, username)
             );

             CREATE INDEX IF NOT EXISTS idx_posts_user_created_id
                 ON posts (username, created_at DESC, id DESC);
             CREATE INDEX IF NOT EXISTS idx_posts_like_count_id
                 ON posts (like_count DESC, id DESC);
             CREATE INDEX IF NOT EXISTS idx_likes_username
                 ON likes (username);",
        )
        .await
        .map_err(io_other)
}

fn create_pool_from_env() -> io::Result<Pool> {
    let host = env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string());
    let port = env::var("DB_PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(5432);
    let user = env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string());
    let password = env::var("DB_PASSWORD").unwrap_or_else(|_| "postgres".to_string());
    let dbname = env::var("DB_NAME").unwrap_or_else(|_| "postgres".to_string());
    let pool_size = env::var("DB_POOL_SIZE")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|size| *size > 0)
        .unwrap_or(64);

    let mut cfg = Config::new();
    cfg.host = Some(host);
    cfg.port = Some(port);
    cfg.user = Some(user);
    cfg.password = Some(password);
    cfg.dbname = Some(dbname);
    cfg.manager = Some(ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    });
    cfg.pool = Some(PoolConfig::new(pool_size));

    cfg.create_pool(Some(Runtime::Tokio1), NoTls)
        .map_err(|err| io::Error::new(io::ErrorKind::Other, err.to_string()))
}

fn io_other<E: fmt::Display>(err: E) -> io::Error {
    io::Error::new(io::ErrorKind::Other, err.to_string())
}

#[actix_web::main]
async fn main() -> io::Result<()> {
    env_logger::init_from_env(env_logger::Env::default().default_filter_or("info"));

    let port = env::var("PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(5001);
    let bind_addr = format!("0.0.0.0:{port}");

    let pool = create_pool_from_env()?;
    init_db(&pool).await.map_err(io_other)?;

    let state = web::Data::new(AppState { pool });
    HttpServer::new(move || {
        App::new()
            .app_data(state.clone())
            .wrap(Logger::default())
            .route("/users", web::post().to(create_user))
            .route("/posts", web::post().to(create_post))
            .route("/follow", web::post().to(follow_user))
            .route("/posts/{postId}/like", web::post().to(like_post))
            .route("/feed", web::get().to(get_feed))
            .route("/trending", web::get().to(get_trending))
    })
    .bind(bind_addr)?
    .run()
    .await
}
