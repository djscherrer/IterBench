use actix_web::{web, App, HttpResponse, HttpServer, Responder};
use chrono::{DateTime, Utc};
use deadpool_postgres::{Config, ManagerConfig, Pool, RecyclingMethod, Runtime};
use serde::{Deserialize, Serialize};
use std::env;
use tokio_postgres::NoTls;

const PAGE_SIZE: i64 = 50;

#[derive(Deserialize)]
struct CreateUserReq {
    username: Option<String>,
    full_name: Option<String>,
    bio: Option<String>,
}

#[derive(Deserialize)]
struct CreatePostReq {
    username: Option<String>,
    content: Option<String>,
}

#[derive(Deserialize)]
struct FollowReq {
    follower_username: Option<String>,
    following_username: Option<String>,
}

#[derive(Deserialize)]
struct LikeReq {
    username: Option<String>,
}

#[derive(Deserialize)]
struct FeedQuery {
    username: String,
    page: Option<i64>,
}

#[derive(Deserialize)]
struct TrendingQuery {
    page: Option<i64>,
}

#[derive(Serialize)]
struct FeedItem {
    id: i64,
    username: String,
    content: String,
    created_at: DateTime<Utc>,
    like_count: i64,
}

#[derive(Serialize)]
struct TrendingItem {
    id: i64,
    username: String,
    content: String,
    like_count: i64,
}

#[derive(Serialize)]
struct FeedResponse {
    items: Vec<FeedItem>,
    page: i64,
    page_size: i64,
    has_next: bool,
}

#[derive(Serialize)]
struct TrendingResponse {
    items: Vec<TrendingItem>,
    page: i64,
    page_size: i64,
    has_next: bool,
}

async fn init_db(pool: &Pool) -> Result<(), Box<dyn std::error::Error>> {
    let client = pool.get().await?;
    client
        .batch_execute(
            "
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                full_name TEXT NOT NULL,
                bio TEXT
            );
            CREATE TABLE IF NOT EXISTS posts (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                like_count BIGINT NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_posts_user_id ON posts(user_id);
            CREATE INDEX IF NOT EXISTS idx_posts_trending ON posts(like_count DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at DESC, id DESC);
            CREATE TABLE IF NOT EXISTS follows (
                follower_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                following_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                PRIMARY KEY (follower_id, following_id)
            );
            CREATE INDEX IF NOT EXISTS idx_follows_follower ON follows(follower_id);
            CREATE TABLE IF NOT EXISTS likes (
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                PRIMARY KEY (user_id, post_id)
            );
            ",
        )
        .await?;
    Ok(())
}

async fn create_user(pool: web::Data<Pool>, body: web::Json<CreateUserReq>) -> impl Responder {
    let username = match body.username.as_ref().filter(|s| !s.is_empty()) {
        Some(u) => u,
        None => return HttpResponse::BadRequest().finish(),
    };
    let full_name = match body.full_name.as_ref().filter(|s| !s.is_empty()) {
        Some(f) => f,
        None => return HttpResponse::BadRequest().finish(),
    };
    let bio = body.bio.as_deref().unwrap_or("");

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let res = client
        .execute(
            "INSERT INTO users (username, full_name, bio) VALUES ($1, $2, $3)",
            &[username, full_name, &bio],
        )
        .await;
    match res {
        Ok(_) => HttpResponse::Created().finish(),
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

async fn create_post(pool: web::Data<Pool>, body: web::Json<CreatePostReq>) -> impl Responder {
    let username = match body.username.as_ref().filter(|s| !s.is_empty()) {
        Some(u) => u,
        None => return HttpResponse::BadRequest().finish(),
    };
    let content = match body.content.as_ref() {
        Some(c) => c,
        None => return HttpResponse::BadRequest().finish(),
    };

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let row = client
        .query_opt("SELECT id FROM users WHERE username = $1", &[username])
        .await;
    let user_id: i64 = match row {
        Ok(Some(r)) => r.get(0),
        _ => return HttpResponse::BadRequest().finish(),
    };
    let res = client
        .execute(
            "INSERT INTO posts (user_id, content) VALUES ($1, $2)",
            &[&user_id, content],
        )
        .await;
    match res {
        Ok(_) => HttpResponse::Created().finish(),
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

async fn follow_user(pool: web::Data<Pool>, body: web::Json<FollowReq>) -> impl Responder {
    let follower = match body.follower_username.as_ref().filter(|s| !s.is_empty()) {
        Some(u) => u,
        None => return HttpResponse::BadRequest().finish(),
    };
    let following = match body.following_username.as_ref().filter(|s| !s.is_empty()) {
        Some(u) => u,
        None => return HttpResponse::BadRequest().finish(),
    };
    if follower == following {
        return HttpResponse::BadRequest().finish();
    }

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let names = vec![follower.clone(), following.clone()];
    let rows = client
        .query(
            "SELECT username, id FROM users WHERE username = ANY($1)",
            &[&names],
        )
        .await;
    let rows = match rows {
        Ok(r) => r,
        Err(_) => return HttpResponse::BadRequest().finish(),
    };
    let mut follower_id: Option<i64> = None;
    let mut following_id: Option<i64> = None;
    for r in &rows {
        let u: String = r.get(0);
        let id: i64 = r.get(1);
        if &u == follower {
            follower_id = Some(id);
        }
        if &u == following {
            following_id = Some(id);
        }
    }
    let (fid, gid) = match (follower_id, following_id) {
        (Some(a), Some(b)) => (a, b),
        _ => return HttpResponse::BadRequest().finish(),
    };
    let res = client
        .execute(
            "INSERT INTO follows (follower_id, following_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            &[&fid, &gid],
        )
        .await;
    match res {
        Ok(n) if n > 0 => HttpResponse::Created().finish(),
        Ok(_) => HttpResponse::Ok().finish(),
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

async fn like_post(
    pool: web::Data<Pool>,
    path: web::Path<i64>,
    body: web::Json<LikeReq>,
) -> impl Responder {
    let post_id = path.into_inner();
    let username = match body.username.as_ref().filter(|s| !s.is_empty()) {
        Some(u) => u,
        None => return HttpResponse::BadRequest().finish(),
    };

    let mut client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let tx = match client.transaction().await {
        Ok(t) => t,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let user_row = match tx
        .query_opt("SELECT id FROM users WHERE username = $1", &[username])
        .await
    {
        Ok(r) => r,
        Err(_) => return HttpResponse::BadRequest().finish(),
    };
    let user_id: i64 = match user_row {
        Some(r) => r.get(0),
        None => return HttpResponse::BadRequest().finish(),
    };

    let post_exists = match tx
        .query_opt("SELECT 1 FROM posts WHERE id = $1", &[&post_id])
        .await
    {
        Ok(r) => r.is_some(),
        Err(_) => return HttpResponse::BadRequest().finish(),
    };
    if !post_exists {
        return HttpResponse::BadRequest().finish();
    }

    let inserted = match tx
        .execute(
            "INSERT INTO likes (user_id, post_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            &[&user_id, &post_id],
        )
        .await
    {
        Ok(n) => n,
        Err(_) => return HttpResponse::BadRequest().finish(),
    };

    if inserted > 0 {
        if tx
            .execute(
                "UPDATE posts SET like_count = like_count + 1 WHERE id = $1",
                &[&post_id],
            )
            .await
            .is_err()
        {
            return HttpResponse::InternalServerError().finish();
        }
    }

    if tx.commit().await.is_err() {
        return HttpResponse::InternalServerError().finish();
    }

    if inserted > 0 {
        HttpResponse::Created().finish()
    } else {
        HttpResponse::Ok().finish()
    }
}

async fn get_feed(pool: web::Data<Pool>, q: web::Query<FeedQuery>) -> impl Responder {
    let page = q.page.unwrap_or(1).max(1);
    let offset = (page - 1) * PAGE_SIZE;
    let limit = PAGE_SIZE + 1;

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let user_row = match client
        .query_opt("SELECT id FROM users WHERE username = $1", &[&q.username])
        .await
    {
        Ok(r) => r,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let user_id: i64 = match user_row {
        Some(r) => r.get(0),
        None => {
            return HttpResponse::Ok().json(FeedResponse {
                items: vec![],
                page,
                page_size: PAGE_SIZE,
                has_next: false,
            })
        }
    };

    let rows = match client
        .query(
            "SELECT p.id, u.username, p.content, p.created_at, p.like_count
             FROM posts p
             JOIN users u ON u.id = p.user_id
             WHERE p.user_id IN (SELECT following_id FROM follows WHERE follower_id = $1)
             ORDER BY p.created_at DESC, p.id DESC
             LIMIT $2 OFFSET $3",
            &[&user_id, &limit, &offset],
        )
        .await
    {
        Ok(r) => r,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let has_next = rows.len() as i64 > PAGE_SIZE;
    let items: Vec<FeedItem> = rows
        .iter()
        .take(PAGE_SIZE as usize)
        .map(|r| FeedItem {
            id: r.get(0),
            username: r.get(1),
            content: r.get(2),
            created_at: r.get(3),
            like_count: r.get(4),
        })
        .collect();

    HttpResponse::Ok().json(FeedResponse {
        items,
        page,
        page_size: PAGE_SIZE,
        has_next,
    })
}

async fn get_trending(pool: web::Data<Pool>, q: web::Query<TrendingQuery>) -> impl Responder {
    let page = q.page.unwrap_or(1).max(1);
    let offset = (page - 1) * PAGE_SIZE;
    let limit = PAGE_SIZE + 1;

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let rows = match client
        .query(
            "SELECT p.id, u.username, p.content, p.like_count
             FROM posts p
             JOIN users u ON u.id = p.user_id
             ORDER BY p.like_count DESC, p.id DESC
             LIMIT $1 OFFSET $2",
            &[&limit, &offset],
        )
        .await
    {
        Ok(r) => r,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let has_next = rows.len() as i64 > PAGE_SIZE;
    let items: Vec<TrendingItem> = rows
        .iter()
        .take(PAGE_SIZE as usize)
        .map(|r| TrendingItem {
            id: r.get(0),
            username: r.get(1),
            content: r.get(2),
            like_count: r.get(3),
        })
        .collect();

    HttpResponse::Ok().json(TrendingResponse {
        items,
        page,
        page_size: PAGE_SIZE,
        has_next,
    })
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let db_host = env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string());
    let db_port: u16 = env::var("DB_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(5432);
    let db_user = env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string());
    let db_password = env::var("DB_PASSWORD").unwrap_or_else(|_| "postgres".to_string());
    let db_name = env::var("DB_NAME").unwrap_or_else(|_| "postgres".to_string());

    let mut cfg = Config::new();
    cfg.host = Some(db_host);
    cfg.port = Some(db_port);
    cfg.user = Some(db_user);
    cfg.password = Some(db_password);
    cfg.dbname = Some(db_name);
    cfg.manager = Some(ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    });

    let pool = cfg
        .create_pool(Some(Runtime::Tokio1), NoTls)
        .expect("Failed to create pool");

    for i in 0..30 {
        match init_db(&pool).await {
            Ok(_) => break,
            Err(e) => {
                if i == 29 {
                    panic!("Failed to init DB: {}", e);
                }
                tokio::time::sleep(std::time::Duration::from_secs(1)).await;
            }
        }
    }

    let port: u16 = env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(5001);

    let pool_data = web::Data::new(pool);

    HttpServer::new(move || {
        App::new()
            .app_data(pool_data.clone())
            .app_data(web::JsonConfig::default().error_handler(|_, _| {
                actix_web::error::InternalError::from_response(
                    "",
                    HttpResponse::BadRequest().finish(),
                )
                .into()
            }))
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
