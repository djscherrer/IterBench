use actix_web::{web, App, HttpResponse, HttpServer};
use chrono::{DateTime, Utc};
use deadpool_postgres::{Config, Pool, Runtime};
use serde::{Deserialize, Serialize};
use std::env;
use tokio_postgres::NoTls;

// ── Configuration ──────────────────────────────────────────────────────────

const PAGE_SIZE: i64 = 50;

fn db_config() -> Config {
    let host = env::var("DB_HOST").unwrap_or_else(|_| "localhost".into());
    let port: u16 = env::var("DB_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(5432);
    let user = env::var("DB_USER").unwrap_or_else(|_| "postgres".into());
    let password = env::var("DB_PASSWORD").unwrap_or_else(|_| "postgres".into());
    let dbname = env::var("DB_NAME").unwrap_or_else(|_| "testdb".into());

    let mut cfg = Config::new();
    cfg.host = Some(host);
    cfg.port = Some(port);
    cfg.user = Some(user);
    cfg.password = Some(password);
    cfg.dbname = Some(dbname);
    cfg
}

async fn init_db(pool: &Pool) -> Result<(), tokio_postgres::Error> {
    let client = pool.get().await.unwrap();
    client
        .batch_execute(
            "
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(255) UNIQUE NOT NULL,
            full_name VARCHAR(255) NOT NULL,
            bio TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS posts (
            id SERIAL PRIMARY KEY,
            username VARCHAR(255) NOT NULL,
            content TEXT NOT NULL,
            like_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS follows (
            follower_username VARCHAR(255) NOT NULL,
            following_username VARCHAR(255) NOT NULL,
            PRIMARY KEY (follower_username, following_username)
        );

        CREATE TABLE IF NOT EXISTS likes (
            username VARCHAR(255) NOT NULL,
            post_id INTEGER NOT NULL REFERENCES posts(id),
            PRIMARY KEY (username, post_id)
        );

        CREATE INDEX IF NOT EXISTS idx_posts_username ON posts(username);
        CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_posts_like_count ON posts(like_count DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_follows_follower ON follows(follower_username);
        CREATE INDEX IF NOT EXISTS idx_follows_following ON follows(following_username);
        CREATE INDEX IF NOT EXISTS idx_likes_post ON likes(post_id);
        ",
        )
        .await?;
    Ok(())
}

// ── Data types ─────────────────────────────────────────────────────────────

#[derive(Serialize)]
struct UserResponse {
    id: i32,
    username: String,
    full_name: String,
    bio: String,
}

#[derive(Serialize)]
struct PostResponse {
    id: i32,
    username: String,
    content: String,
    like_count: i32,
    created_at: DateTime<Utc>,
}

#[derive(Serialize)]
struct FeedItemResponse {
    id: i32,
    username: String,
    content: String,
    created_at: DateTime<Utc>,
    like_count: i32,
}

#[derive(Serialize)]
struct TrendingItemResponse {
    id: i32,
    username: String,
    content: String,
    like_count: i32,
}

#[derive(Serialize)]
struct PaginatedResponse<T: Serialize> {
    items: Vec<T>,
    page: i64,
    page_size: i64,
    has_next: bool,
}

#[derive(Deserialize)]
struct CreateUserRequest {
    username: String,
    full_name: String,
    #[serde(default)]
    bio: String,
}

#[derive(Deserialize)]
struct CreatePostRequest {
    username: String,
    content: String,
}

#[derive(Deserialize)]
struct LikeRequest {
    username: String,
}

#[derive(Deserialize)]
struct FollowRequest {
    follower_username: String,
    following_username: String,
}

#[derive(Deserialize)]
struct FeedQuery {
    username: String,
    #[serde(default = "default_page")]
    page: i64,
}

fn default_page() -> i64 {
    1
}

#[derive(Deserialize)]
struct TrendingQuery {
    #[serde(default = "default_page")]
    page: i64,
}

// ── Handlers ───────────────────────────────────────────────────────────────

async fn create_user(
    pool: web::Data<Pool>,
    body: web::Json<CreateUserRequest>,
) -> HttpResponse {
    if body.username.trim().is_empty() || body.full_name.trim().is_empty() {
        return HttpResponse::BadRequest().json(serde_json::json!({
            "error": "username and full_name are required"
        }));
    }

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => {
            return HttpResponse::InternalServerError().json(serde_json::json!({
                "error": "database connection error"
            }));
        }
    };

    let result = client
        .query_one(
            "INSERT INTO users (username, full_name, bio) VALUES ($1, $2, $3) RETURNING id, username, full_name, bio",
            &[&body.username, &body.full_name, &body.bio],
        )
        .await;

    match result {
        Ok(row) => {
            let user = UserResponse {
                id: row.get("id"),
                username: row.get("username"),
                full_name: row.get("full_name"),
                bio: row.get("bio"),
            };
            HttpResponse::Created().json(user)
        }
        Err(e) => {
            if let Some(db_err) = e.as_db_error() {
                if db_err.code() == &tokio_postgres::error::SqlState::UNIQUE_VIOLATION {
                    return HttpResponse::BadRequest().json(serde_json::json!({
                        "error": "username already exists"
                    }));
                }
            }
            HttpResponse::BadRequest().json(serde_json::json!({
                "error": format!("{}", e)
            }))
        }
    }
}

async fn create_post(
    pool: web::Data<Pool>,
    body: web::Json<CreatePostRequest>,
) -> HttpResponse {
    if body.username.trim().is_empty() || body.content.trim().is_empty() {
        return HttpResponse::BadRequest().json(serde_json::json!({
            "error": "username and content are required"
        }));
    }

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => {
            return HttpResponse::InternalServerError().json(serde_json::json!({
                "error": "database connection error"
            }));
        }
    };

    let result = client
        .query_one(
            "INSERT INTO posts (username, content) VALUES ($1, $2) RETURNING id, username, content, like_count, created_at",
            &[&body.username, &body.content],
        )
        .await;

    match result {
        Ok(row) => {
            let post = PostResponse {
                id: row.get("id"),
                username: row.get("username"),
                content: row.get("content"),
                like_count: row.get("like_count"),
                created_at: row.get("created_at"),
            };
            HttpResponse::Created().json(post)
        }
        Err(e) => HttpResponse::BadRequest().json(serde_json::json!({
            "error": format!("{}", e)
        })),
    }
}

async fn like_post(
    pool: web::Data<Pool>,
    path: web::Path<i32>,
    body: web::Json<LikeRequest>,
) -> HttpResponse {
    let post_id = path.into_inner();

    if body.username.trim().is_empty() {
        return HttpResponse::BadRequest().json(serde_json::json!({
            "error": "username is required"
        }));
    }

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => {
            return HttpResponse::InternalServerError().json(serde_json::json!({
                "error": "database connection error"
            }));
        }
    };

    // Check if post exists
    let post_exists = client
        .query_opt("SELECT id FROM posts WHERE id = $1", &[&post_id])
        .await;

    match post_exists {
        Ok(None) => {
            return HttpResponse::BadRequest().json(serde_json::json!({
                "error": "post not found"
            }));
        }
        Err(e) => {
            return HttpResponse::InternalServerError().json(serde_json::json!({
                "error": format!("{}", e)
            }));
        }
        _ => {}
    }

    // Try to insert like; if duplicate, just return 200
    let insert_result = client
        .execute(
            "INSERT INTO likes (username, post_id) VALUES ($1, $2) ON CONFLICT (username, post_id) DO NOTHING",
            &[&body.username, &post_id],
        )
        .await;

    match insert_result {
        Ok(rows_affected) => {
            if rows_affected > 0 {
                // New like — increment like_count
                let _ = client
                    .execute(
                        "UPDATE posts SET like_count = like_count + 1 WHERE id = $1",
                        &[&post_id],
                    )
                    .await;
                HttpResponse::Created().json(serde_json::json!({
                    "message": "liked"
                }))
            } else {
                // Already liked
                HttpResponse::Ok().json(serde_json::json!({
                    "message": "already liked"
                }))
            }
        }
        Err(e) => HttpResponse::BadRequest().json(serde_json::json!({
            "error": format!("{}", e)
        })),
    }
}

async fn follow_user(
    pool: web::Data<Pool>,
    body: web::Json<FollowRequest>,
) -> HttpResponse {
    if body.follower_username.trim().is_empty() || body.following_username.trim().is_empty() {
        return HttpResponse::BadRequest().json(serde_json::json!({
            "error": "follower_username and following_username are required"
        }));
    }

    if body.follower_username == body.following_username {
        return HttpResponse::BadRequest().json(serde_json::json!({
            "error": "cannot follow yourself"
        }));
    }

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => {
            return HttpResponse::InternalServerError().json(serde_json::json!({
                "error": "database connection error"
            }));
        }
    };

    let result = client
        .execute(
            "INSERT INTO follows (follower_username, following_username) VALUES ($1, $2) ON CONFLICT (follower_username, following_username) DO NOTHING",
            &[&body.follower_username, &body.following_username],
        )
        .await;

    match result {
        Ok(rows_affected) => {
            if rows_affected > 0 {
                HttpResponse::Created().json(serde_json::json!({
                    "message": "followed"
                }))
            } else {
                HttpResponse::Ok().json(serde_json::json!({
                    "message": "already following"
                }))
            }
        }
        Err(e) => HttpResponse::BadRequest().json(serde_json::json!({
            "error": format!("{}", e)
        })),
    }
}

async fn get_feed(
    pool: web::Data<Pool>,
    query: web::Query<FeedQuery>,
) -> HttpResponse {
    let page = std::cmp::max(1, query.page);
    let offset = (page - 1) * PAGE_SIZE;

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => {
            return HttpResponse::InternalServerError().json(serde_json::json!({
                "error": "database connection error"
            }));
        }
    };

    // Fetch PAGE_SIZE + 1 to determine has_next
    let rows = client
        .query(
            "SELECT p.id, p.username, p.content, p.like_count, p.created_at \
             FROM posts p \
             INNER JOIN follows f ON p.username = f.following_username \
             WHERE f.follower_username = $1 \
             ORDER BY p.created_at DESC, p.id DESC \
             LIMIT $2 OFFSET $3",
            &[&query.username, &(PAGE_SIZE + 1), &offset],
        )
        .await;

    match rows {
        Ok(rows) => {
            let has_next = rows.len() > PAGE_SIZE as usize;
            let items: Vec<FeedItemResponse> = rows
                .iter()
                .take(PAGE_SIZE as usize)
                .map(|r| FeedItemResponse {
                    id: r.get("id"),
                    username: r.get("username"),
                    content: r.get("content"),
                    created_at: r.get("created_at"),
                    like_count: r.get("like_count"),
                })
                .collect();

            HttpResponse::Ok().json(PaginatedResponse {
                items,
                page,
                page_size: PAGE_SIZE,
                has_next,
            })
        }
        Err(e) => HttpResponse::InternalServerError().json(serde_json::json!({
            "error": format!("{}", e)
        })),
    }
}

async fn get_trending(
    pool: web::Data<Pool>,
    query: web::Query<TrendingQuery>,
) -> HttpResponse {
    let page = std::cmp::max(1, query.page);
    let offset = (page - 1) * PAGE_SIZE;

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => {
            return HttpResponse::InternalServerError().json(serde_json::json!({
                "error": "database connection error"
            }));
        }
    };

    let rows = client
        .query(
            "SELECT id, username, content, like_count \
             FROM posts \
             ORDER BY like_count DESC, id DESC \
             LIMIT $1 OFFSET $2",
            &[&(PAGE_SIZE + 1), &offset],
        )
        .await;

    match rows {
        Ok(rows) => {
            let has_next = rows.len() > PAGE_SIZE as usize;
            let items: Vec<TrendingItemResponse> = rows
                .iter()
                .take(PAGE_SIZE as usize)
                .map(|r| TrendingItemResponse {
                    id: r.get("id"),
                    username: r.get("username"),
                    content: r.get("content"),
                    like_count: r.get("like_count"),
                })
                .collect();

            HttpResponse::Ok().json(PaginatedResponse {
                items,
                page,
                page_size: PAGE_SIZE,
                has_next,
            })
        }
        Err(e) => HttpResponse::InternalServerError().json(serde_json::json!({
            "error": format!("{}", e)
        })),
    }
}

// ── Main ───────────────────────────────────────────────────────────────────

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let port: u16 = env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(5001);

    let pool = db_config()
        .create_pool(Some(Runtime::Tokio1), NoTls)
        .expect("failed to create database pool");

    // Initialize database tables
    init_db(&pool).await.expect("failed to initialize database");

    println!("Starting server on 0.0.0.0:{}", port);

    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(pool.clone()))
            .route("/users", web::post().to(create_user))
            .route("/posts", web::post().to(create_post))
            .route("/posts/{postId}/like", web::post().to(like_post))
            .route("/follow", web::post().to(follow_user))
            .route("/feed", web::get().to(get_feed))
            .route("/trending", web::get().to(get_trending))
    })
    .bind(("0.0.0.0", port))?
    .run()
    .await
}
