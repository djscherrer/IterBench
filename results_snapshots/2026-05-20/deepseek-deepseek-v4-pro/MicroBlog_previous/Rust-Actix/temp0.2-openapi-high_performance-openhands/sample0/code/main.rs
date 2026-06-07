use actix_web::{web, App, HttpServer, HttpResponse};
use deadpool_postgres::{Config, Pool, Runtime};
use serde::{Deserialize, Serialize};
use tokio_postgres::NoTls;
use std::env;

// ── Database initialization ──

async fn init_db(pool: &Pool) -> Result<(), Box<dyn std::error::Error>> {
    let client = pool.get().await?;
    client.batch_execute(
        "
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(255) UNIQUE NOT NULL,
            full_name VARCHAR(255) NOT NULL,
            bio TEXT DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS posts (
            id SERIAL PRIMARY KEY,
            username VARCHAR(255) NOT NULL REFERENCES users(username) ON DELETE CASCADE,
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            like_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_posts_username_created ON posts(username, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_posts_likecount_id ON posts(like_count DESC, id DESC);

        CREATE TABLE IF NOT EXISTS follows (
            follower_username VARCHAR(255) NOT NULL REFERENCES users(username) ON DELETE CASCADE,
            following_username VARCHAR(255) NOT NULL REFERENCES users(username) ON DELETE CASCADE,
            PRIMARY KEY (follower_username, following_username)
        );

        CREATE INDEX IF NOT EXISTS idx_follows_follower ON follows(follower_username);

        CREATE TABLE IF NOT EXISTS likes (
            post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
            username VARCHAR(255) NOT NULL REFERENCES users(username) ON DELETE CASCADE,
            PRIMARY KEY (post_id, username)
        );
        "
    ).await?;
    Ok(())
}

// ── Models ──

#[derive(Debug, Serialize, Deserialize)]
struct CreateUserRequest {
    username: String,
    full_name: String,
    #[serde(default)]
    bio: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
struct CreatePostRequest {
    username: String,
    content: String,
}

#[derive(Debug, Serialize, Deserialize)]
struct FollowRequest {
    follower_username: String,
    following_username: String,
}

#[derive(Debug, Serialize, Deserialize)]
struct LikeRequest {
    username: String,
}

#[derive(Debug, Serialize)]
struct PostItem {
    id: i32,
    username: String,
    content: String,
    created_at: chrono::DateTime<chrono::Utc>,
    like_count: i32,
}

#[derive(Debug, Serialize)]
struct PaginatedResponse {
    items: Vec<PostItem>,
    page: i32,
    page_size: i32,
    has_next: bool,
}

#[derive(Debug, Serialize)]
struct TrendingPostItem {
    id: i32,
    username: String,
    content: String,
    like_count: i32,
}

#[derive(Debug, Serialize)]
struct TrendingPaginatedResponse {
    items: Vec<TrendingPostItem>,
    page: i32,
    page_size: i32,
    has_next: bool,
}

#[derive(Debug, Deserialize)]
struct FeedQuery {
    username: String,
    page: Option<i32>,
}

#[derive(Debug, Deserialize)]
struct TrendingQuery {
    page: Option<i32>,
}

// ── Handlers ──

const PAGE_SIZE: i64 = 50;

type DbPool = web::Data<Pool>;

async fn create_user(
    pool: DbPool,
    body: web::Json<CreateUserRequest>,
) -> HttpResponse {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let bio = body.bio.as_deref().unwrap_or("");
    let result = client
        .execute(
            "INSERT INTO users (username, full_name, bio) VALUES ($1, $2, $3)",
            &[&body.username, &body.full_name, &bio],
        )
        .await;

    match result {
        Ok(_) => HttpResponse::Created().finish(),
        Err(e) => {
            if e.as_db_error().map(|d| d.code().code()).as_deref() == Some("23505") {
                HttpResponse::BadRequest().json(serde_json::json!({"error": "Username already exists"}))
            } else {
                HttpResponse::BadRequest().json(serde_json::json!({"error": "Invalid input"}))
            }
        }
    }
}

async fn create_post(
    pool: DbPool,
    body: web::Json<CreatePostRequest>,
) -> HttpResponse {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let result = client
        .execute(
            "INSERT INTO posts (username, content) VALUES ($1, $2)",
            &[&body.username, &body.content],
        )
        .await;

    match result {
        Ok(_) => HttpResponse::Created().finish(),
        Err(e) => {
            if e.as_db_error().map(|d| d.code().code()).as_deref() == Some("23503") {
                HttpResponse::BadRequest().json(serde_json::json!({"error": "User does not exist"}))
            } else {
                HttpResponse::BadRequest().json(serde_json::json!({"error": "Invalid input"}))
            }
        }
    }
}

async fn follow_user(
    pool: DbPool,
    body: web::Json<FollowRequest>,
) -> HttpResponse {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let result = client
        .execute(
            "INSERT INTO follows (follower_username, following_username) VALUES ($1, $2)",
            &[&body.follower_username, &body.following_username],
        )
        .await;

    match result {
        Ok(_) => HttpResponse::Created().finish(),
        Err(e) => {
            if e.as_db_error().map(|d| d.code().code()).as_deref() == Some("23505") {
                HttpResponse::Created().finish()
            } else {
                HttpResponse::BadRequest().json(serde_json::json!({"error": "Invalid input"}))
            }
        }
    }
}

async fn like_post(
    pool: DbPool,
    path: web::Path<i32>,
    body: web::Json<LikeRequest>,
) -> HttpResponse {
    let post_id = path.into_inner();
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    // Insert into likes, ignore duplicates
    let _ = client
        .execute(
            "INSERT INTO likes (post_id, username) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            &[&post_id, &body.username],
        )
        .await;

    // Update like_count based on actual likes count
    let _ = client
        .execute(
            "UPDATE posts SET like_count = (SELECT COUNT(*) FROM likes WHERE post_id = $1) WHERE id = $1",
            &[&post_id],
        )
        .await;

    HttpResponse::Created().finish()
}

async fn get_feed(
    pool: DbPool,
    query: web::Query<FeedQuery>,
) -> HttpResponse {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let page = query.page.unwrap_or(1).max(1);
    let offset = (page as i64 - 1) * PAGE_SIZE;
    let limit = PAGE_SIZE + 1; // fetch one extra to determine has_next

    let rows = client
        .query(
            "SELECT p.id, p.username, p.content, p.created_at, p.like_count
             FROM posts p
             JOIN follows f ON p.username = f.following_username
             WHERE f.follower_username = $1
             ORDER BY p.created_at DESC
             LIMIT $2 OFFSET $3",
            &[&query.username, &limit, &offset],
        )
        .await;

    let rows = match rows {
        Ok(r) => r,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let has_next = rows.len() as i64 > PAGE_SIZE;
    let items: Vec<PostItem> = rows
        .into_iter()
        .take(PAGE_SIZE as usize)
        .map(|row| PostItem {
            id: row.get("id"),
            username: row.get("username"),
            content: row.get("content"),
            created_at: row.get("created_at"),
            like_count: row.get("like_count"),
        })
        .collect();

    HttpResponse::Ok().json(PaginatedResponse {
        items,
        page,
        page_size: PAGE_SIZE as i32,
        has_next,
    })
}

async fn get_trending(
    pool: DbPool,
    query: web::Query<TrendingQuery>,
) -> HttpResponse {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let page = query.page.unwrap_or(1).max(1);
    let offset = (page as i64 - 1) * PAGE_SIZE;
    let limit = PAGE_SIZE + 1;

    let rows = client
        .query(
            "SELECT id, username, content, like_count
             FROM posts
             ORDER BY like_count DESC, id DESC
             LIMIT $1 OFFSET $2",
            &[&limit, &offset],
        )
        .await;

    let rows = match rows {
        Ok(r) => r,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let has_next = rows.len() as i64 > PAGE_SIZE;
    let items: Vec<TrendingPostItem> = rows
        .into_iter()
        .take(PAGE_SIZE as usize)
        .map(|row| TrendingPostItem {
            id: row.get("id"),
            username: row.get("username"),
            content: row.get("content"),
            like_count: row.get("like_count"),
        })
        .collect();

    HttpResponse::Ok().json(TrendingPaginatedResponse {
        items,
        page,
        page_size: PAGE_SIZE as i32,
        has_next,
    })
}

// ── Main ──

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let db_host = env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string());
    let db_port: u16 = env::var("DB_PORT")
        .unwrap_or_else(|_| "5432".to_string())
        .parse()
        .unwrap_or(5432);
    let db_user = env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string());
    let db_password = env::var("DB_PASSWORD").unwrap_or_else(|_| "postgres".to_string());
    let db_name = env::var("DB_NAME").unwrap_or_else(|_| "testdb".to_string());
    let port: u16 = env::var("PORT")
        .unwrap_or_else(|_| "5001".to_string())
        .parse()
        .unwrap_or(5001);

    let mut cfg = Config::new();
    cfg.host = Some(db_host);
    cfg.port = Some(db_port);
    cfg.user = Some(db_user);
    cfg.password = Some(db_password);
    cfg.dbname = Some(db_name);

    let pool = cfg
        .create_pool(Some(Runtime::Tokio1), NoTls)
        .expect("Failed to create database pool");

    // Initialize database tables
    init_db(&pool).await.expect("Failed to initialize database");

    println!("Starting server on 0.0.0.0:{}", port);

    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(pool.clone()))
            .route("/users", web::post().to(create_user))
            .route("/posts", web::post().to(create_post))
            .route("/follow", web::post().to(follow_user))
            .route("/posts/{postId}/like", web::post().to(like_post))
            .route("/feed", web::get().to(get_feed))
            .route("/trending", web::get().to(get_trending))
    })
    .bind(("0.0.0.0", port))?
    .run()
    .await
}
