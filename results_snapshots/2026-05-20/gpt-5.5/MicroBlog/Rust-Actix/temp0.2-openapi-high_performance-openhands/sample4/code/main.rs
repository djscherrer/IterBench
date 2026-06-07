use actix_web::{web, App, HttpResponse, HttpServer};
use chrono::{DateTime, Utc};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use serde::{Deserialize, Serialize};
use std::env;
use tokio_postgres::{Config as PgConfig, NoTls};

const PAGE_SIZE: i64 = 50;

#[derive(Clone)]
struct AppState {
    pool: Pool,
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
struct PageResponse<T> {
    items: Vec<T>,
    page: i64,
    page_size: i64,
    has_next: bool,
}

fn is_blank(value: &str) -> bool {
    value.trim().is_empty()
}

fn bad_request() -> HttpResponse {
    HttpResponse::BadRequest().finish()
}

fn internal_error<E: std::fmt::Display>(err: E) -> HttpResponse {
    eprintln!("internal server error: {err}");
    HttpResponse::InternalServerError().finish()
}

fn parse_page(page: Option<i64>) -> Result<(i64, i64), HttpResponse> {
    let page = page.unwrap_or(1);
    if page < 1 {
        return Err(bad_request());
    }
    let offset = page
        .checked_sub(1)
        .and_then(|p| p.checked_mul(PAGE_SIZE))
        .ok_or_else(bad_request)?;
    Ok((page, offset))
}

fn pg_config_from_env() -> PgConfig {
    let host = env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string());
    let port = env::var("DB_PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(5432);
    let user = env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string());
    let password = env::var("DB_PASSWORD").unwrap_or_default();
    let dbname = env::var("DB_NAME").unwrap_or_else(|_| "postgres".to_string());

    let mut config = PgConfig::new();
    config.host(&host);
    config.port(port);
    config.user(&user);
    config.password(&password);
    config.dbname(&dbname);
    config
}

fn create_pool() -> Pool {
    let max_size = env::var("DB_POOL_SIZE")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(64);
    let manager_config = ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    };
    let manager = Manager::from_config(pg_config_from_env(), NoTls, manager_config);
    Pool::builder(manager)
        .max_size(max_size)
        .build()
        .expect("failed to create PostgreSQL connection pool")
}

async fn initialize_database(pool: &Pool) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
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
                like_count BIGINT NOT NULL DEFAULT 0
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
            CREATE INDEX IF NOT EXISTS idx_follows_follower_following
                ON follows (follower_username, following_username);
            CREATE INDEX IF NOT EXISTS idx_post_likes_username
                ON post_likes (username);
            ",
        )
        .await?;
    Ok(())
}

async fn create_user(
    state: web::Data<AppState>,
    body: web::Json<CreateUserRequest>,
) -> HttpResponse {
    if is_blank(&body.username) || is_blank(&body.full_name) {
        return bad_request();
    }

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(err) => return internal_error(err),
    };

    match client
        .query_opt(
            "INSERT INTO users (username, full_name, bio)
             VALUES ($1, $2, $3)
             ON CONFLICT (username) DO NOTHING
             RETURNING username",
            &[&body.username, &body.full_name, &body.bio],
        )
        .await
    {
        Ok(Some(_)) => HttpResponse::Created().finish(),
        Ok(None) => bad_request(),
        Err(err) => internal_error(err),
    }
}

async fn create_post(
    state: web::Data<AppState>,
    body: web::Json<CreatePostRequest>,
) -> HttpResponse {
    if is_blank(&body.username) || is_blank(&body.content) {
        return bad_request();
    }

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(err) => return internal_error(err),
    };

    match client
        .query_opt(
            "INSERT INTO posts (username, content)
             SELECT $1, $2
             WHERE EXISTS (SELECT 1 FROM users WHERE username = $1)
             RETURNING id",
            &[&body.username, &body.content],
        )
        .await
    {
        Ok(Some(_)) => HttpResponse::Created().finish(),
        Ok(None) => bad_request(),
        Err(err) => internal_error(err),
    }
}

async fn follow_user(state: web::Data<AppState>, body: web::Json<FollowRequest>) -> HttpResponse {
    if is_blank(&body.follower_username) || is_blank(&body.following_username) {
        return bad_request();
    }

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(err) => return internal_error(err),
    };

    match client
        .query_one(
            "WITH status AS (
                 SELECT
                     EXISTS (SELECT 1 FROM users WHERE username = $1) AS follower_exists,
                     EXISTS (SELECT 1 FROM users WHERE username = $2) AS following_exists
             ), inserted AS (
                 INSERT INTO follows (follower_username, following_username)
                 SELECT $1, $2 FROM status
                 WHERE follower_exists AND following_exists
                 ON CONFLICT DO NOTHING
                 RETURNING follower_username
             )
             SELECT
                 (SELECT COUNT(*) FROM inserted) AS inserted,
                 follower_exists,
                 following_exists
             FROM status",
            &[&body.follower_username, &body.following_username],
        )
        .await
    {
        Ok(row) => {
            let inserted: i64 = row.get("inserted");
            let follower_exists: bool = row.get("follower_exists");
            let following_exists: bool = row.get("following_exists");
            if !follower_exists || !following_exists {
                bad_request()
            } else if inserted == 1 {
                HttpResponse::Created().finish()
            } else {
                HttpResponse::Ok().finish()
            }
        }
        Err(err) => internal_error(err),
    }
}

async fn like_post(
    state: web::Data<AppState>,
    path: web::Path<i64>,
    body: web::Json<LikeRequest>,
) -> HttpResponse {
    let post_id = path.into_inner();
    if post_id < 1 || is_blank(&body.username) {
        return bad_request();
    }

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(err) => return internal_error(err),
    };

    match client
        .query_one(
            "WITH status AS (
                 SELECT
                     EXISTS (SELECT 1 FROM users WHERE username = $2) AS user_exists,
                     EXISTS (SELECT 1 FROM posts WHERE id = $1) AS post_exists
             ), inserted AS (
                 INSERT INTO post_likes (post_id, username)
                 SELECT $1, $2 FROM status
                 WHERE user_exists AND post_exists
                 ON CONFLICT DO NOTHING
                 RETURNING post_id
             ), updated AS (
                 UPDATE posts
                 SET like_count = like_count + 1
                 WHERE id = $1 AND EXISTS (SELECT 1 FROM inserted)
                 RETURNING id
             )
             SELECT
                 (SELECT COUNT(*) FROM inserted) AS inserted,
                 user_exists,
                 post_exists
             FROM status",
            &[&post_id, &body.username],
        )
        .await
    {
        Ok(row) => {
            let inserted: i64 = row.get("inserted");
            let user_exists: bool = row.get("user_exists");
            let post_exists: bool = row.get("post_exists");
            if !user_exists || !post_exists {
                bad_request()
            } else if inserted == 1 {
                HttpResponse::Created().finish()
            } else {
                HttpResponse::Ok().finish()
            }
        }
        Err(err) => internal_error(err),
    }
}

async fn get_feed(state: web::Data<AppState>, query: web::Query<FeedQuery>) -> HttpResponse {
    if is_blank(&query.username) {
        return bad_request();
    }
    let (page, offset) = match parse_page(query.page) {
        Ok(value) => value,
        Err(response) => return response,
    };

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(err) => return internal_error(err),
    };

    match client
        .query(
            "SELECT p.id, p.username, p.content, p.created_at, p.like_count
             FROM follows f
             JOIN posts p ON p.username = f.following_username
             WHERE f.follower_username = $1
             ORDER BY p.created_at DESC, p.id DESC
             LIMIT $2 OFFSET $3",
            &[&query.username, &(PAGE_SIZE + 1), &offset],
        )
        .await
    {
        Ok(rows) => {
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
        Err(err) => internal_error(err),
    }
}

async fn get_trending(state: web::Data<AppState>, query: web::Query<PageQuery>) -> HttpResponse {
    let (page, offset) = match parse_page(query.page) {
        Ok(value) => value,
        Err(response) => return response,
    };

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(err) => return internal_error(err),
    };

    match client
        .query(
            "SELECT id, username, content, like_count
             FROM posts
             ORDER BY like_count DESC, id DESC
             LIMIT $1 OFFSET $2",
            &[&(PAGE_SIZE + 1), &offset],
        )
        .await
    {
        Ok(rows) => {
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
        Err(err) => internal_error(err),
    }
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let pool = create_pool();
    if let Err(err) = initialize_database(&pool).await {
        eprintln!("failed to initialize database: {err}");
        return Err(std::io::Error::new(
            std::io::ErrorKind::Other,
            "database initialization failed",
        ));
    }

    let port = env::var("PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(5001);
    let address = format!("0.0.0.0:{port}");
    let state = AppState { pool };

    println!("listening on {address}");
    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(state.clone()))
            .route("/users", web::post().to(create_user))
            .route("/posts", web::post().to(create_post))
            .route("/follow", web::post().to(follow_user))
            .route("/posts/{postId}/like", web::post().to(like_post))
            .route("/feed", web::get().to(get_feed))
            .route("/trending", web::get().to(get_trending))
    })
    .bind(address)?
    .run()
    .await
}
