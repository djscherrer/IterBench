use actix_web::{web, App, HttpResponse, HttpServer, Responder};
use chrono::{DateTime, Utc};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use serde::{Deserialize, Serialize};
use std::{env, io};
use tokio_postgres::{error::SqlState, NoTls};

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

fn blank(value: &str) -> bool {
    value.trim().is_empty()
}

fn page_or_bad_request(page: Option<i64>) -> Result<i64, HttpResponse> {
    let page = page.unwrap_or(1);
    if page < 1 {
        Err(HttpResponse::BadRequest().finish())
    } else {
        Ok(page)
    }
}

fn db_error_response(error: &tokio_postgres::Error) -> HttpResponse {
    match error.code() {
        Some(&SqlState::UNIQUE_VIOLATION)
        | Some(&SqlState::FOREIGN_KEY_VIOLATION)
        | Some(&SqlState::CHECK_VIOLATION)
        | Some(&SqlState::NOT_NULL_VIOLATION) => HttpResponse::BadRequest().finish(),
        _ => HttpResponse::InternalServerError().finish(),
    }
}

async fn create_user(state: web::Data<AppState>, request: web::Json<CreateUserRequest>) -> impl Responder {
    if blank(&request.username) || blank(&request.full_name) {
        return HttpResponse::BadRequest().finish();
    }

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query_opt(
            "INSERT INTO users (username, full_name, bio) VALUES ($1, $2, $3) \
             ON CONFLICT (username) DO NOTHING RETURNING username",
            &[&request.username, &request.full_name, &request.bio],
        )
        .await
    {
        Ok(Some(_)) => HttpResponse::Created().finish(),
        Ok(None) => HttpResponse::BadRequest().finish(),
        Err(error) => db_error_response(&error),
    }
}

async fn create_post(state: web::Data<AppState>, request: web::Json<CreatePostRequest>) -> impl Responder {
    if blank(&request.username) || blank(&request.content) {
        return HttpResponse::BadRequest().finish();
    }

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .execute(
            "INSERT INTO posts (username, content) VALUES ($1, $2)",
            &[&request.username, &request.content],
        )
        .await
    {
        Ok(_) => HttpResponse::Created().finish(),
        Err(error) => db_error_response(&error),
    }
}

async fn follow_user(state: web::Data<AppState>, request: web::Json<FollowRequest>) -> impl Responder {
    if blank(&request.follower_username) || blank(&request.following_username) {
        return HttpResponse::BadRequest().finish();
    }

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query_opt(
            "INSERT INTO follows (follower_username, following_username) VALUES ($1, $2) \
             ON CONFLICT (follower_username, following_username) DO NOTHING RETURNING 1",
            &[&request.follower_username, &request.following_username],
        )
        .await
    {
        Ok(Some(_)) => HttpResponse::Created().finish(),
        Ok(None) => HttpResponse::Ok().finish(),
        Err(error) => db_error_response(&error),
    }
}

async fn like_post(
    state: web::Data<AppState>,
    post_id: web::Path<i64>,
    request: web::Json<LikeRequest>,
) -> impl Responder {
    let post_id = post_id.into_inner();
    if post_id < 1 || blank(&request.username) {
        return HttpResponse::BadRequest().finish();
    }

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query_one(
            "WITH inserted AS ( \
                 INSERT INTO likes (post_id, username) VALUES ($1, $2) \
                 ON CONFLICT (post_id, username) DO NOTHING \
                 RETURNING 1 \
              ), updated AS ( \
                 UPDATE posts SET like_count = like_count + 1 \
                 WHERE id = $1 AND EXISTS (SELECT 1 FROM inserted) \
                 RETURNING 1 \
              ) \
              SELECT EXISTS (SELECT 1 FROM inserted) AS inserted",
            &[&post_id, &request.username],
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
        Err(error) => db_error_response(&error),
    }
}

async fn get_feed(state: web::Data<AppState>, query: web::Query<FeedQuery>) -> impl Responder {
    if blank(&query.username) {
        return HttpResponse::BadRequest().finish();
    }
    let page = match page_or_bad_request(query.page) {
        Ok(page) => page,
        Err(response) => return response,
    };
    let offset = (page - 1) * PAGE_SIZE;
    let limit = PAGE_SIZE + 1;

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query(
            "SELECT p.id, p.username, p.content, p.created_at, p.like_count \
             FROM follows f \
             JOIN posts p ON p.username = f.following_username \
             WHERE f.follower_username = $1 \
             ORDER BY p.created_at DESC, p.id DESC \
             LIMIT $2 OFFSET $3",
            &[&query.username, &limit, &offset],
        )
        .await
    {
        Ok(rows) => {
            let has_next = rows.len() > PAGE_SIZE as usize;
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
        Err(error) => db_error_response(&error),
    }
}

async fn get_trending(state: web::Data<AppState>, query: web::Query<PageQuery>) -> impl Responder {
    let page = match page_or_bad_request(query.page) {
        Ok(page) => page,
        Err(response) => return response,
    };
    let offset = (page - 1) * PAGE_SIZE;
    let limit = PAGE_SIZE + 1;

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query(
            "SELECT id, username, content, like_count \
             FROM posts \
             ORDER BY like_count DESC, id DESC \
             LIMIT $1 OFFSET $2",
            &[&limit, &offset],
        )
        .await
    {
        Ok(rows) => {
            let has_next = rows.len() > PAGE_SIZE as usize;
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
        Err(error) => db_error_response(&error),
    }
}

fn env_string(name: &str, default: &str) -> String {
    env::var(name).unwrap_or_else(|_| default.to_owned())
}

fn create_pool() -> io::Result<Pool> {
    let mut pg_config = tokio_postgres::Config::new();
    pg_config.host(&env_string("DB_HOST", "localhost"));
    pg_config.port(env_string("DB_PORT", "5432").parse::<u16>().unwrap_or(5432));
    pg_config.user(&env_string("DB_USER", "postgres"));
    pg_config.password(env_string("DB_PASSWORD", "postgres"));
    pg_config.dbname(&env_string("DB_NAME", "postgres"));

    let manager_config = ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    };
    let manager = Manager::from_config(pg_config, NoTls, manager_config);
    let max_size = env::var("DB_POOL_SIZE")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(64);

    Pool::builder(manager)
        .max_size(max_size)
        .build()
        .map_err(|error| io::Error::new(io::ErrorKind::Other, error.to_string()))
}

async fn init_db(pool: &Pool) -> io::Result<()> {
    let client = pool
        .get()
        .await
        .map_err(|error| io::Error::new(io::ErrorKind::Other, error.to_string()))?;

    client
        .batch_execute(
            "CREATE TABLE IF NOT EXISTS users ( \
                username TEXT PRIMARY KEY, \
                full_name TEXT NOT NULL, \
                bio TEXT, \
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW() \
             ); \
             CREATE TABLE IF NOT EXISTS posts ( \
                id BIGSERIAL PRIMARY KEY, \
                username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE, \
                content TEXT NOT NULL, \
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), \
                like_count BIGINT NOT NULL DEFAULT 0 \
             ); \
             CREATE TABLE IF NOT EXISTS follows ( \
                follower_username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE, \
                following_username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE, \
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), \
                PRIMARY KEY (follower_username, following_username) \
             ); \
             CREATE TABLE IF NOT EXISTS likes ( \
                post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE, \
                username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE, \
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), \
                PRIMARY KEY (post_id, username) \
             ); \
             CREATE INDEX IF NOT EXISTS idx_posts_feed ON posts (username, created_at DESC, id DESC); \
             CREATE INDEX IF NOT EXISTS idx_posts_trending ON posts (like_count DESC, id DESC); \
             CREATE INDEX IF NOT EXISTS idx_follows_following ON follows (following_username); \
             CREATE INDEX IF NOT EXISTS idx_likes_username ON likes (username);",
        )
        .await
        .map_err(|error| io::Error::new(io::ErrorKind::Other, error.to_string()))
}

#[actix_web::main]
async fn main() -> io::Result<()> {
    env_logger::init();

    let port = env::var("PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(5001);
    let bind_addr = format!("0.0.0.0:{port}");

    let pool = create_pool()?;
    init_db(&pool).await?;
    let state = AppState { pool };

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
    .bind(bind_addr)?
    .run()
    .await
}
