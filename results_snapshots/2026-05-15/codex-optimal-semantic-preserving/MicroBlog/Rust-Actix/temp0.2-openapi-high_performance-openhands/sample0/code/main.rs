use actix_web::{web, App, HttpServer, HttpResponse};
use deadpool_postgres::{Config, Pool, Runtime, ManagerConfig, RecyclingMethod};
use serde::{Deserialize, Serialize};
use tokio_postgres::NoTls;
use chrono::{DateTime, Utc};
use std::env;

#[derive(Serialize)]
struct PostResponse {
    id: i64,
    username: String,
    content: String,
    like_count: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    created_at: Option<DateTime<Utc>>,
}

#[derive(Deserialize)]
struct CreateUserRequest {
    username: Option<String>,
    full_name: Option<String>,
    bio: Option<String>,
}

#[derive(Deserialize)]
struct CreatePostRequest {
    username: Option<String>,
    content: Option<String>,
}

#[derive(Deserialize)]
struct FollowRequest {
    follower_username: Option<String>,
    following_username: Option<String>,
}

#[derive(Deserialize)]
struct LikeRequest {
    username: Option<String>,
}

#[derive(Deserialize)]
struct FeedQuery {
    username: Option<String>,
}

async fn init_db(pool: &Pool) {
    let client = pool.get().await.expect("Failed to get DB connection for init");

    client.batch_execute("
        CREATE TABLE IF NOT EXISTS users (
            username VARCHAR(255) PRIMARY KEY,
            full_name VARCHAR(255) NOT NULL,
            bio TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS posts (
            id BIGSERIAL PRIMARY KEY,
            username VARCHAR(255) NOT NULL REFERENCES users(username),
            content TEXT NOT NULL,
            like_count BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS follows (
            follower_username VARCHAR(255) NOT NULL REFERENCES users(username),
            following_username VARCHAR(255) NOT NULL REFERENCES users(username),
            PRIMARY KEY (follower_username, following_username)
        );

        CREATE TABLE IF NOT EXISTS likes (
            post_id BIGINT NOT NULL REFERENCES posts(id),
            username VARCHAR(255) NOT NULL REFERENCES users(username),
            PRIMARY KEY (post_id, username)
        );

        CREATE INDEX IF NOT EXISTS idx_posts_feed_covering
            ON posts(username, created_at DESC)
            INCLUDE (id, content, like_count);
        CREATE INDEX IF NOT EXISTS idx_posts_trending_covering
            ON posts(like_count DESC, created_at DESC)
            INCLUDE (id, username, content);
    ").await.expect("Failed to initialize database tables");
}

async fn create_user(
    pool: web::Data<Pool>,
    body: web::Json<CreateUserRequest>,
) -> HttpResponse {
    let username = match &body.username {
        Some(u) if !u.is_empty() => u,
        _ => return HttpResponse::BadRequest().finish(),
    };
    let full_name = match &body.full_name {
        Some(f) if !f.is_empty() => f,
        _ => return HttpResponse::BadRequest().finish(),
    };
    let bio = body.bio.as_deref().unwrap_or("");

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client.execute(
        "INSERT INTO users (username, full_name, bio) VALUES ($1, $2, $3) ON CONFLICT (username) DO NOTHING",
        &[username, full_name, &bio],
    ).await {
        Ok(0) => HttpResponse::BadRequest().finish(),
        Ok(_) => HttpResponse::Created().finish(),
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

async fn create_post(
    pool: web::Data<Pool>,
    body: web::Json<CreatePostRequest>,
) -> HttpResponse {
    let username = match &body.username {
        Some(u) if !u.is_empty() => u,
        _ => return HttpResponse::BadRequest().finish(),
    };
    let content = match &body.content {
        Some(c) if !c.is_empty() => c,
        _ => return HttpResponse::BadRequest().finish(),
    };

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client.execute(
        "INSERT INTO posts (username, content) VALUES ($1, $2)",
        &[username, content],
    ).await {
        Ok(_) => HttpResponse::Created().finish(),
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

async fn follow_user(
    pool: web::Data<Pool>,
    body: web::Json<FollowRequest>,
) -> HttpResponse {
    let follower = match &body.follower_username {
        Some(u) if !u.is_empty() => u,
        _ => return HttpResponse::BadRequest().finish(),
    };
    let following = match &body.following_username {
        Some(u) if !u.is_empty() => u,
        _ => return HttpResponse::BadRequest().finish(),
    };

    if follower == following {
        return HttpResponse::BadRequest().finish();
    }

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client.execute(
        "INSERT INTO follows (follower_username, following_username) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        &[follower, following],
    ).await {
        Ok(_) => HttpResponse::Created().finish(),
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

async fn like_post(
    pool: web::Data<Pool>,
    path: web::Path<i64>,
    body: web::Json<LikeRequest>,
) -> HttpResponse {
    let post_id = path.into_inner();
    let username = match &body.username {
        Some(u) if !u.is_empty() => u.clone(),
        _ => return HttpResponse::BadRequest().finish(),
    };

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    // Keep the generated sample's persistence semantics: the like insert
    // determines the response, while a following count update is best-effort.
    let result = client.execute(
        "INSERT INTO likes (post_id, username) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        &[&post_id, &username],
    ).await;

    match result {
        Ok(rows) => {
            if rows > 0 {
                let _ = client.execute(
                    "UPDATE posts SET like_count = like_count + 1 WHERE id = $1",
                    &[&post_id],
                ).await;
            }
            HttpResponse::Created().finish()
        }
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

async fn get_feed(
    pool: web::Data<Pool>,
    query: web::Query<FeedQuery>,
) -> HttpResponse {
    let username = match &query.username {
        Some(u) if !u.is_empty() => u,
        _ => return HttpResponse::Ok().json(Vec::<PostResponse>::new()),
    };

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let rows = match client.query(
        "WITH visible_users(username) AS (
             SELECT $1::VARCHAR(255)
             UNION
             SELECT f.following_username
             FROM follows f
             WHERE f.follower_username = $1
         )
         SELECT p.id, p.username, p.content, p.like_count, p.created_at
         FROM visible_users vu
         JOIN LATERAL (
             SELECT p.id, p.username, p.content, p.like_count, p.created_at
             FROM posts p
             WHERE p.username = vu.username
             ORDER BY p.created_at DESC
             LIMIT 100
         ) p ON TRUE
         ORDER BY p.created_at DESC
         LIMIT 100",
        &[username],
    ).await {
        Ok(rows) => rows,
        Err(_) => return HttpResponse::Ok().json(Vec::<PostResponse>::new()),
    };

    let posts: Vec<PostResponse> = rows.iter().map(|row| {
        PostResponse {
            id: row.get::<_, i64>("id"),
            username: row.get("username"),
            content: row.get("content"),
            like_count: row.get::<_, i64>("like_count"),
            created_at: Some(row.get::<_, DateTime<Utc>>("created_at")),
        }
    }).collect();

    HttpResponse::Ok().json(posts)
}

async fn get_trending(
    pool: web::Data<Pool>,
) -> HttpResponse {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let rows = match client.query(
        "SELECT id, username, content, like_count
         FROM posts
         ORDER BY like_count DESC, created_at DESC
         LIMIT 10",
        &[],
    ).await {
        Ok(rows) => rows,
        Err(_) => return HttpResponse::Ok().json(Vec::<PostResponse>::new()),
    };

    let posts: Vec<PostResponse> = rows.iter().map(|row| {
        PostResponse {
            id: row.get::<_, i64>("id"),
            username: row.get("username"),
            content: row.get("content"),
            like_count: row.get::<_, i64>("like_count"),
            created_at: None,
        }
    }).collect();

    HttpResponse::Ok().json(posts)
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let db_host = env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string());
    let db_port: u16 = env::var("DB_PORT").unwrap_or_else(|_| "5432".to_string()).parse().unwrap_or(5432);
    let db_user = env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string());
    let db_password = env::var("DB_PASSWORD").unwrap_or_else(|_| "postgres".to_string());
    let db_name = env::var("DB_NAME").unwrap_or_else(|_| "testdb".to_string());
    let port: u16 = env::var("PORT").unwrap_or_else(|_| "5001".to_string()).parse().unwrap_or(5001);

    let mut cfg = Config::new();
    cfg.host = Some(db_host);
    cfg.port = Some(db_port);
    cfg.user = Some(db_user);
    cfg.password = Some(db_password);
    cfg.dbname = Some(db_name);
    cfg.manager = Some(ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    });
    cfg.pool = Some(deadpool_postgres::PoolConfig {
        max_size: 64,
        ..Default::default()
    });

    let pool = cfg.create_pool(Some(Runtime::Tokio1), NoTls)
        .expect("Failed to create pool");

    init_db(&pool).await;

    let pool_data = web::Data::new(pool);

    HttpServer::new(move || {
        App::new()
            .app_data(pool_data.clone())
            .app_data(web::JsonConfig::default().error_handler(|err, _req| {
                actix_web::error::InternalError::from_response(
                    err,
                    HttpResponse::BadRequest().finish(),
                ).into()
            }))
            .route("/users", web::post().to(create_user))
            .route("/posts", web::post().to(create_post))
            .route("/follow", web::post().to(follow_user))
            .route("/posts/{postId}/like", web::post().to(like_post))
            .route("/feed", web::get().to(get_feed))
            .route("/trending", web::get().to(get_trending))
    })
    .workers(num_cpus())
    .bind(("0.0.0.0", port))?
    .run()
    .await
}

fn num_cpus() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4)
}
