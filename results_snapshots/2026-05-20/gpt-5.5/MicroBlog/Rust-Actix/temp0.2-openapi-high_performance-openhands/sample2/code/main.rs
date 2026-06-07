use actix_web::{web, App, HttpResponse, HttpServer, Responder};
use chrono::{DateTime, Utc};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod, Runtime};
use serde::{Deserialize, Serialize};
use std::{env, io};
use tokio_postgres::NoTls;

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

fn env_or_default(name: &str, default: &str) -> String {
    env::var(name).unwrap_or_else(|_| default.to_string())
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

fn page_and_offset(page: Option<i64>) -> Option<(i64, i64)> {
    let page = page.unwrap_or(1);
    if page < 1 {
        return None;
    }
    let offset = page.checked_sub(1)?.checked_mul(PAGE_SIZE)?;
    Some((page, offset))
}

fn create_pool() -> io::Result<Pool> {
    let mut cfg = tokio_postgres::Config::new();
    cfg.host(&env_or_default("DB_HOST", "localhost"));
    cfg.port(env_or_default("DB_PORT", "5432").parse().unwrap_or(5432));
    cfg.user(&env_or_default("DB_USER", "postgres"));
    cfg.password(&env_or_default("DB_PASSWORD", "postgres"));
    cfg.dbname(&env_or_default("DB_NAME", "postgres"));

    let manager = Manager::from_config(
        cfg,
        NoTls,
        ManagerConfig {
            recycling_method: RecyclingMethod::Fast,
        },
    );
    Pool::builder(manager)
        .max_size(
            env::var("DB_POOL_SIZE")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(32),
        )
        .runtime(Runtime::Tokio1)
        .build()
        .map_err(|err| io::Error::new(io::ErrorKind::Other, err))
}

async fn init_db(pool: &Pool) -> io::Result<()> {
    let client = pool
        .get()
        .await
        .map_err(|err| io::Error::new(io::ErrorKind::Other, err))?;

    client
        .batch_execute(
            r#"
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                full_name TEXT NOT NULL,
                bio TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CHECK (btrim(username) <> ''),
                CHECK (btrim(full_name) <> '')
            );

            CREATE TABLE IF NOT EXISTS posts (
                id BIGSERIAL PRIMARY KEY,
                username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                like_count INTEGER NOT NULL DEFAULT 0 CHECK (like_count >= 0),
                CHECK (btrim(content) <> '')
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
            CREATE INDEX IF NOT EXISTS idx_posts_trending
                ON posts (like_count DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_likes_username
                ON likes (username);
            "#,
        )
        .await
        .map_err(|err| io::Error::new(io::ErrorKind::Other, err))
}

async fn create_user(
    state: web::Data<AppState>,
    body: web::Json<CreateUserRequest>,
) -> impl Responder {
    let username = body.username.trim();
    let full_name = body.full_name.trim();
    if username.is_empty() || full_name.is_empty() {
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
            &[&username, &full_name, &body.bio.as_deref().unwrap_or("")],
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
) -> impl Responder {
    let username = body.username.trim();
    if username.is_empty() || is_blank(&body.content) {
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
            &[&username, &body.content],
        )
        .await
    {
        Ok(Some(_)) => HttpResponse::Created().finish(),
        Ok(None) => bad_request(),
        Err(err) => internal_error(err),
    }
}

async fn follow_user(state: web::Data<AppState>, body: web::Json<FollowRequest>) -> impl Responder {
    let follower = body.follower_username.trim();
    let following = body.following_username.trim();
    if follower.is_empty() || following.is_empty() {
        return bad_request();
    }

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(err) => return internal_error(err),
    };

    match client
        .query_one(
            "WITH checks AS (
                 SELECT
                   EXISTS (SELECT 1 FROM users WHERE username = $1) AS follower_exists,
                   EXISTS (SELECT 1 FROM users WHERE username = $2) AS following_exists
             ), inserted AS (
                 INSERT INTO follows (follower_username, following_username)
                 SELECT $1, $2 FROM checks
                 WHERE follower_exists AND following_exists
                 ON CONFLICT (follower_username, following_username) DO NOTHING
                 RETURNING 1
             )
             SELECT follower_exists, following_exists,
                    EXISTS (SELECT 1 FROM inserted) AS inserted
             FROM checks",
            &[&follower, &following],
        )
        .await
    {
        Ok(row) => {
            let follower_exists: bool = row.get(0);
            let following_exists: bool = row.get(1);
            let inserted: bool = row.get(2);
            if !follower_exists || !following_exists {
                bad_request()
            } else if inserted {
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
) -> impl Responder {
    let post_id = path.into_inner();
    let username = body.username.trim();
    if post_id <= 0 || username.is_empty() {
        return bad_request();
    }

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(err) => return internal_error(err),
    };

    match client
        .query_one(
            "WITH checks AS (
                 SELECT
                   EXISTS (SELECT 1 FROM posts WHERE id = $1) AS post_exists,
                   EXISTS (SELECT 1 FROM users WHERE username = $2) AS user_exists
             ), inserted AS (
                 INSERT INTO likes (post_id, username)
                 SELECT $1, $2 FROM checks
                 WHERE post_exists AND user_exists
                 ON CONFLICT (post_id, username) DO NOTHING
                 RETURNING 1
             ), updated AS (
                 UPDATE posts
                 SET like_count = like_count + 1
                 WHERE id = $1 AND EXISTS (SELECT 1 FROM inserted)
                 RETURNING 1
             )
             SELECT post_exists, user_exists,
                    EXISTS (SELECT 1 FROM inserted) AS inserted
             FROM checks",
            &[&post_id, &username],
        )
        .await
    {
        Ok(row) => {
            let post_exists: bool = row.get(0);
            let user_exists: bool = row.get(1);
            let inserted: bool = row.get(2);
            if !post_exists || !user_exists {
                bad_request()
            } else if inserted {
                HttpResponse::Created().finish()
            } else {
                HttpResponse::Ok().finish()
            }
        }
        Err(err) => internal_error(err),
    }
}

async fn get_feed(state: web::Data<AppState>, query: web::Query<FeedQuery>) -> impl Responder {
    let username = query.username.trim();
    let (page, offset) = match page_and_offset(query.page) {
        Some(values) if !username.is_empty() => values,
        _ => return bad_request(),
    };
    let limit = PAGE_SIZE + 1;

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(err) => return internal_error(err),
    };

    match client
        .query(
            "SELECT p.id, p.username, p.content, p.created_at, p.like_count
             FROM posts p
             JOIN follows f ON f.following_username = p.username
             WHERE f.follower_username = $1
             ORDER BY p.created_at DESC, p.id DESC
             LIMIT $2 OFFSET $3",
            &[&username, &limit, &offset],
        )
        .await
    {
        Ok(rows) => {
            let has_next = rows.len() as i64 > PAGE_SIZE;
            let items = rows
                .into_iter()
                .take(PAGE_SIZE as usize)
                .map(|row| FeedPost {
                    id: row.get(0),
                    username: row.get(1),
                    content: row.get(2),
                    created_at: row.get(3),
                    like_count: row.get(4),
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

async fn get_trending(state: web::Data<AppState>, query: web::Query<PageQuery>) -> impl Responder {
    let (page, offset) = match page_and_offset(query.page) {
        Some(values) => values,
        None => return bad_request(),
    };
    let limit = PAGE_SIZE + 1;

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
            &[&limit, &offset],
        )
        .await
    {
        Ok(rows) => {
            let has_next = rows.len() as i64 > PAGE_SIZE;
            let items = rows
                .into_iter()
                .take(PAGE_SIZE as usize)
                .map(|row| TrendingPost {
                    id: row.get(0),
                    username: row.get(1),
                    content: row.get(2),
                    like_count: row.get(3),
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
async fn main() -> io::Result<()> {
    env_logger::init();

    let pool = create_pool()?;
    init_db(&pool).await?;

    let port: u16 = env::var("PORT")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(5001);
    let state = web::Data::new(AppState { pool });

    HttpServer::new(move || {
        App::new()
            .app_data(state.clone())
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
