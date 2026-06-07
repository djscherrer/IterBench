use actix_web::error::ErrorInternalServerError;
use actix_web::{web, App, HttpResponse, HttpServer};
use chrono::{DateTime, Utc};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use serde::{Deserialize, Serialize};
use std::env;
use std::io;
use tokio_postgres::NoTls;

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
struct FollowRequest {
    follower_username: String,
    following_username: String,
}

#[derive(Deserialize)]
struct CreatePostRequest {
    username: String,
    content: String,
}

#[derive(Deserialize)]
struct LikePostRequest {
    username: String,
}

#[derive(Deserialize)]
struct FeedQuery {
    username: String,
}

#[derive(Serialize)]
struct FeedPost {
    id: i32,
    username: String,
    content: String,
    created_at: DateTime<Utc>,
    like_count: i32,
}

#[derive(Serialize)]
struct TrendingPost {
    id: i32,
    username: String,
    content: String,
    like_count: i32,
}

fn normalize_required(value: &str) -> Option<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_owned())
    }
}

fn normalize_optional(value: Option<String>) -> String {
    value.unwrap_or_default().trim().to_owned()
}

fn build_pool_from_env() -> Result<Pool, Box<dyn std::error::Error + Send + Sync>> {
    let mut config = tokio_postgres::Config::new();
    config.host(&env::var("DB_HOST")?);
    config.port(env::var("DB_PORT")?.parse::<u16>()?);
    config.user(&env::var("DB_USER")?);
    config.password(&env::var("DB_PASSWORD")?);
    config.dbname(&env::var("DB_NAME")?);

    let manager = Manager::from_config(
        config,
        NoTls,
        ManagerConfig {
            recycling_method: RecyclingMethod::Fast,
        },
    );

    let max_pool_size = std::thread::available_parallelism()
        .map(|parallelism| parallelism.get().saturating_mul(4))
        .unwrap_or(16)
        .clamp(16, 64);

    let pool = Pool::builder(manager).max_size(max_pool_size).build()?;
    Ok(pool)
}

async fn initialize_database(pool: &Pool) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let client = pool.get().await?;
    client
        .batch_execute(
            r#"
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                full_name TEXT NOT NULL,
                bio TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                username TEXT NOT NULL REFERENCES users(username),
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                like_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS follows (
                follower_username TEXT NOT NULL REFERENCES users(username),
                following_username TEXT NOT NULL REFERENCES users(username),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (follower_username, following_username),
                CHECK (follower_username <> following_username)
            );

            CREATE TABLE IF NOT EXISTS post_likes (
                post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                username TEXT NOT NULL REFERENCES users(username),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (post_id, username)
            );

            CREATE INDEX IF NOT EXISTS idx_posts_user_created
                ON posts (username, created_at DESC, id DESC);

            CREATE INDEX IF NOT EXISTS idx_posts_trending
                ON posts (like_count DESC, created_at DESC, id DESC);
            "#,
        )
        .await?;
    Ok(())
}

async fn create_user(
    state: web::Data<AppState>,
    request: web::Json<CreateUserRequest>,
) -> actix_web::Result<HttpResponse> {
    let username = normalize_required(&request.username)
        .ok_or_else(|| actix_web::error::ErrorBadRequest("username is required"))?;
    let full_name = normalize_required(&request.full_name)
        .ok_or_else(|| actix_web::error::ErrorBadRequest("full_name is required"))?;
    let bio = normalize_optional(request.bio.clone());

    let client = state.pool.get().await.map_err(ErrorInternalServerError)?;
    let row = client
        .query_opt(
            r#"
            INSERT INTO users (username, full_name, bio)
            VALUES ($1, $2, $3)
            ON CONFLICT (username) DO NOTHING
            RETURNING username
            "#,
            &[&username, &full_name, &bio],
        )
        .await
        .map_err(ErrorInternalServerError)?;

    if row.is_some() {
        Ok(HttpResponse::Created().finish())
    } else {
        Ok(HttpResponse::BadRequest().body("invalid input or username already exists"))
    }
}

async fn follow_user(
    state: web::Data<AppState>,
    request: web::Json<FollowRequest>,
) -> actix_web::Result<HttpResponse> {
    let follower_username = normalize_required(&request.follower_username)
        .ok_or_else(|| actix_web::error::ErrorBadRequest("follower_username is required"))?;
    let following_username = normalize_required(&request.following_username)
        .ok_or_else(|| actix_web::error::ErrorBadRequest("following_username is required"))?;

    if follower_username == following_username {
        return Ok(HttpResponse::BadRequest().body("cannot follow self"));
    }

    let client = state.pool.get().await.map_err(ErrorInternalServerError)?;
    let row = client
        .query_one(
            r#"
            WITH follower_exists AS (
                SELECT 1 FROM users WHERE username = $1
            ),
            following_exists AS (
                SELECT 1 FROM users WHERE username = $2
            ),
            ins AS (
                INSERT INTO follows (follower_username, following_username)
                SELECT $1, $2
                WHERE EXISTS (SELECT 1 FROM follower_exists)
                  AND EXISTS (SELECT 1 FROM following_exists)
                ON CONFLICT DO NOTHING
                RETURNING 1
            )
            SELECT EXISTS (SELECT 1 FROM follower_exists) AS follower_exists,
                   EXISTS (SELECT 1 FROM following_exists) AS following_exists
            "#,
            &[&follower_username, &following_username],
        )
        .await
        .map_err(ErrorInternalServerError)?;

    let follower_exists: bool = row.get("follower_exists");
    let following_exists: bool = row.get("following_exists");

    if follower_exists && following_exists {
        Ok(HttpResponse::Created().finish())
    } else {
        Ok(HttpResponse::BadRequest().body("invalid input"))
    }
}

async fn create_post(
    state: web::Data<AppState>,
    request: web::Json<CreatePostRequest>,
) -> actix_web::Result<HttpResponse> {
    let username = normalize_required(&request.username)
        .ok_or_else(|| actix_web::error::ErrorBadRequest("username is required"))?;
    let content = normalize_required(&request.content)
        .ok_or_else(|| actix_web::error::ErrorBadRequest("content is required"))?;

    let client = state.pool.get().await.map_err(ErrorInternalServerError)?;
    let row = client
        .query_opt(
            r#"
            INSERT INTO posts (username, content)
            SELECT $1, $2
            WHERE EXISTS (SELECT 1 FROM users WHERE username = $1)
            RETURNING id
            "#,
            &[&username, &content],
        )
        .await
        .map_err(ErrorInternalServerError)?;

    if row.is_some() {
        Ok(HttpResponse::Created().finish())
    } else {
        Ok(HttpResponse::BadRequest().body("invalid input"))
    }
}

async fn like_post(
    state: web::Data<AppState>,
    path: web::Path<i32>,
    request: web::Json<LikePostRequest>,
) -> actix_web::Result<HttpResponse> {
    let post_id = path.into_inner();
    if post_id <= 0 {
        return Ok(HttpResponse::BadRequest().body("invalid input"));
    }

    let username = normalize_required(&request.username)
        .ok_or_else(|| actix_web::error::ErrorBadRequest("username is required"))?;

    let client = state.pool.get().await.map_err(ErrorInternalServerError)?;
    let row = client
        .query_one(
            r#"
            WITH user_exists AS (
                SELECT 1 FROM users WHERE username = $2
            ),
            post_exists AS (
                SELECT 1 FROM posts WHERE id = $1
            ),
            ins AS (
                INSERT INTO post_likes (post_id, username)
                SELECT $1, $2
                WHERE EXISTS (SELECT 1 FROM user_exists)
                  AND EXISTS (SELECT 1 FROM post_exists)
                ON CONFLICT DO NOTHING
                RETURNING 1
            ),
            upd AS (
                UPDATE posts
                SET like_count = like_count + 1
                WHERE id = $1
                  AND EXISTS (SELECT 1 FROM ins)
                RETURNING 1
            )
            SELECT EXISTS (SELECT 1 FROM user_exists) AS user_exists,
                   EXISTS (SELECT 1 FROM post_exists) AS post_exists
            "#,
            &[&post_id, &username],
        )
        .await
        .map_err(ErrorInternalServerError)?;

    let user_exists: bool = row.get("user_exists");
    let post_exists: bool = row.get("post_exists");

    if user_exists && post_exists {
        Ok(HttpResponse::Created().finish())
    } else {
        Ok(HttpResponse::BadRequest().body("invalid input"))
    }
}

async fn get_feed(
    state: web::Data<AppState>,
    query: web::Query<FeedQuery>,
) -> actix_web::Result<HttpResponse> {
    let username = normalize_required(&query.username)
        .ok_or_else(|| actix_web::error::ErrorBadRequest("username is required"))?;

    let client = state.pool.get().await.map_err(ErrorInternalServerError)?;
    let rows = client
        .query(
            r#"
            SELECT p.id, p.username, p.content, p.created_at, p.like_count
            FROM posts p
            JOIN (
                SELECT $1::TEXT AS username
                UNION
                SELECT following_username AS username
                FROM follows
                WHERE follower_username = $1
            ) AS feed_users
              ON feed_users.username = p.username
            ORDER BY p.created_at DESC, p.id DESC
            "#,
            &[&username],
        )
        .await
        .map_err(ErrorInternalServerError)?;

    let feed: Vec<FeedPost> = rows
        .into_iter()
        .map(|row| FeedPost {
            id: row.get("id"),
            username: row.get("username"),
            content: row.get("content"),
            created_at: row.get("created_at"),
            like_count: row.get("like_count"),
        })
        .collect();

    Ok(HttpResponse::Ok().json(feed))
}

async fn get_trending(state: web::Data<AppState>) -> actix_web::Result<HttpResponse> {
    let client = state.pool.get().await.map_err(ErrorInternalServerError)?;
    let rows = client
        .query(
            r#"
            SELECT id, username, content, like_count
            FROM posts
            ORDER BY like_count DESC, created_at DESC, id DESC
            "#,
            &[],
        )
        .await
        .map_err(ErrorInternalServerError)?;

    let posts: Vec<TrendingPost> = rows
        .into_iter()
        .map(|row| TrendingPost {
            id: row.get("id"),
            username: row.get("username"),
            content: row.get("content"),
            like_count: row.get("like_count"),
        })
        .collect();

    Ok(HttpResponse::Ok().json(posts))
}

#[tokio::main]
async fn main() -> io::Result<()> {
    env_logger::init();

    let pool = build_pool_from_env()
        .map_err(|error| io::Error::new(io::ErrorKind::Other, format!("pool init failed: {error}")))?;

    initialize_database(&pool)
        .await
        .map_err(|error| io::Error::new(io::ErrorKind::Other, format!("database init failed: {error}")))?;

    let state = AppState { pool };
    let port = env::var("PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(5001);
    let bind_address = ("0.0.0.0", port);
    let workers = std::thread::available_parallelism()
        .map(|parallelism| parallelism.get())
        .unwrap_or(4);

    HttpServer::new(move || {
        App::new()
            .app_data(web::JsonConfig::default().limit(64 * 1024))
            .app_data(web::Data::new(state.clone()))
            .route("/users", web::post().to(create_user))
            .route("/follow", web::post().to(follow_user))
            .route("/posts", web::post().to(create_post))
            .route("/posts/{postId}/like", web::post().to(like_post))
            .route("/feed", web::get().to(get_feed))
            .route("/trending", web::get().to(get_trending))
    })
    .workers(workers)
    .bind(bind_address)?
    .run()
    .await
}
