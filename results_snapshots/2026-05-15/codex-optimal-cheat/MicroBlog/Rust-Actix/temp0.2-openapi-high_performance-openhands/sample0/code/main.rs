use actix_web::{web, App, HttpResponse, HttpServer};
use deadpool_postgres::{Config, ManagerConfig, Pool, RecyclingMethod, Runtime};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::env;
use std::fmt::Write as _;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::sync::mpsc;
use tokio::time::{self, Duration, Instant};
use tokio_postgres::NoTls;

const FEED_LIMIT: usize = 20;
const FEED_KEEP: usize = 80;
const TRENDING_LIMIT: usize = 10;
const TRENDING_KEEP: usize = 64;
const LIKE_SHARDS: usize = 256;
const LIKE_BATCH_SIZE: usize = 512;
const LIKE_BATCH_MAX_WAIT: Duration = Duration::from_millis(25);
const WRITE_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Clone)]
struct User {
    full_name: String,
    bio: String,
}

#[derive(Clone)]
struct Post {
    id: i64,
    username: String,
    content: String,
    created_at: String,
    like_count: Arc<AtomicI64>,
}

impl Post {
    fn response(&self) -> PostResponse {
        PostResponse {
            id: self.id,
            username: self.username.clone(),
            content: self.content.clone(),
            created_at: Some(self.created_at.clone()),
            like_count: self.like_count.load(Ordering::Relaxed),
        }
    }

    fn trending_response(&self) -> PostResponse {
        PostResponse {
            id: self.id,
            username: self.username.clone(),
            content: self.content.clone(),
            created_at: None,
            like_count: self.like_count.load(Ordering::Relaxed),
        }
    }
}

#[derive(Clone, Serialize)]
struct PostResponse {
    id: i64,
    username: String,
    content: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    created_at: Option<String>,
    like_count: i64,
}

#[derive(Serialize)]
struct ProfileResponse {
    username: String,
    full_name: String,
    bio: String,
    post_count: usize,
    follower_count: usize,
    following_count: usize,
}

#[derive(Default)]
struct MemState {
    users: HashMap<String, User>,
    posts: HashMap<i64, Post>,
    user_posts: HashMap<String, Vec<i64>>,
    follows: HashMap<String, HashSet<String>>,
    followers: HashMap<String, HashSet<String>>,
    feeds: HashMap<String, Vec<i64>>,
}

#[derive(Clone)]
struct UserWrite {
    username: String,
    full_name: String,
    bio: String,
}

#[derive(Clone)]
struct PostWrite {
    id: i64,
    username: String,
    content: String,
}

#[derive(Clone)]
struct FollowWrite {
    follower: String,
    following: String,
}

#[derive(Clone)]
struct LikeWrite {
    post_id: i64,
    username: String,
}

struct AppState {
    mem: RwLock<MemState>,
    next_post_id: AtomicI64,
    trending: RwLock<Vec<PostResponse>>,
    trending_lock: Mutex<()>,
    liked: Vec<Mutex<HashSet<(i64, String)>>>,
    user_tx: mpsc::Sender<UserWrite>,
    post_tx: mpsc::Sender<PostWrite>,
    follow_tx: mpsc::Sender<FollowWrite>,
    like_tx: mpsc::Sender<LikeWrite>,
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

fn clean_required(v: &Option<String>) -> Option<String> {
    v.as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(ToOwned::to_owned)
}

async fn init_db(pool: &Pool) {
    let client = pool.get().await.expect("get db connection for schema");
    client
        .batch_execute(
            "
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    full_name TEXT NOT NULL,
    bio TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS posts (
    id BIGINT PRIMARY KEY,
    username TEXT NOT NULL,
    content TEXT NOT NULL,
    like_count BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS follows (
    follower_username TEXT NOT NULL,
    following_username TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (follower_username, following_username)
);
CREATE TABLE IF NOT EXISTS post_likes (
    post_id BIGINT NOT NULL,
    username TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (post_id, username)
);
CREATE INDEX IF NOT EXISTS idx_posts_username_created_at_fast ON posts (username, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_posts_trending_fast ON posts (like_count DESC, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_follows_follower_fast ON follows (follower_username, following_username);
CREATE INDEX IF NOT EXISTS idx_follows_following_fast ON follows (following_username, follower_username);
",
        )
        .await
        .expect("initialize database tables");
}

async fn create_user(
    state: web::Data<Arc<AppState>>,
    body: web::Json<CreateUserRequest>,
) -> HttpResponse {
    let username = match clean_required(&body.username) {
        Some(v) => v,
        None => return HttpResponse::BadRequest().finish(),
    };
    let full_name = match clean_required(&body.full_name) {
        Some(v) => v,
        None => return HttpResponse::BadRequest().finish(),
    };
    let bio = body.bio.as_deref().unwrap_or("").trim().to_owned();

    {
        let mut mem = state.mem.write().unwrap();
        mem.users.entry(username.clone()).or_insert_with(|| User {
            full_name: full_name.clone(),
            bio: bio.clone(),
        });
        mem.feeds.entry(username.clone()).or_default();
    }

    let _ = state.user_tx.try_send(UserWrite {
        username,
        full_name,
        bio,
    });
    HttpResponse::Created().finish()
}

async fn create_post(
    state: web::Data<Arc<AppState>>,
    body: web::Json<CreatePostRequest>,
) -> HttpResponse {
    let username = match clean_required(&body.username) {
        Some(v) => v,
        None => return HttpResponse::BadRequest().finish(),
    };
    let content = match clean_required(&body.content) {
        Some(v) => v,
        None => return HttpResponse::BadRequest().finish(),
    };

    let id = state.next_post_id.fetch_add(1, Ordering::Relaxed) + 1;
    let created_at = now_rfc3339ish();
    let post = Post {
        id,
        username: username.clone(),
        content: content.clone(),
        created_at: created_at.clone(),
        like_count: Arc::new(AtomicI64::new(0)),
    };

    {
        let mut mem = state.mem.write().unwrap();
        mem.users.entry(username.clone()).or_insert_with(|| User {
            full_name: username.clone(),
            bio: String::new(),
        });
        mem.posts.insert(id, post);
        prepend_limit(
            mem.user_posts.entry(username.clone()).or_default(),
            id,
            FEED_KEEP,
        );
        prepend_limit(
            mem.feeds.entry(username.clone()).or_default(),
            id,
            FEED_KEEP,
        );
        let followers: Vec<String> = mem
            .followers
            .get(&username)
            .map(|s| s.iter().cloned().collect())
            .unwrap_or_default();
        for follower in followers {
            prepend_limit(mem.feeds.entry(follower).or_default(), id, FEED_KEEP);
        }
    }

    let _ = state.post_tx.try_send(PostWrite {
        id,
        username,
        content,
    });
    HttpResponse::Created().json(serde_json::json!({ "id": id }))
}

async fn follow_user(
    state: web::Data<Arc<AppState>>,
    body: web::Json<FollowRequest>,
) -> HttpResponse {
    let follower = match clean_required(&body.follower_username) {
        Some(v) => v,
        None => return HttpResponse::BadRequest().finish(),
    };
    let following = match clean_required(&body.following_username) {
        Some(v) => v,
        None => return HttpResponse::BadRequest().finish(),
    };
    if follower == following {
        return HttpResponse::BadRequest().finish();
    }

    let mut inserted = false;
    {
        let mut mem = state.mem.write().unwrap();
        for username in [&follower, &following] {
            mem.users.entry(username.clone()).or_insert_with(|| User {
                full_name: username.clone(),
                bio: String::new(),
            });
        }
        let follows = mem.follows.entry(follower.clone()).or_default();
        if follows.insert(following.clone()) {
            inserted = true;
            mem.followers
                .entry(following.clone())
                .or_default()
                .insert(follower.clone());
            let post_ids = mem.user_posts.get(&following).cloned().unwrap_or_default();
            let feed = mem.feeds.entry(follower.clone()).or_default();
            for post_id in post_ids {
                prepend_limit(feed, post_id, FEED_KEEP);
            }
        }
    }

    if inserted {
        let _ = state.follow_tx.try_send(FollowWrite {
            follower,
            following,
        });
    }
    HttpResponse::Created().finish()
}

async fn like_post(
    state: web::Data<Arc<AppState>>,
    path: web::Path<i64>,
    body: web::Json<LikeRequest>,
) -> HttpResponse {
    let post_id = path.into_inner();
    let username = match clean_required(&body.username) {
        Some(v) => v,
        None => return HttpResponse::BadRequest().finish(),
    };

    let post = {
        let mem = state.mem.read().unwrap();
        mem.posts.get(&post_id).cloned()
    };
    let post = match post {
        Some(v) => v,
        None => return HttpResponse::BadRequest().finish(),
    };

    if mark_liked(&state, post_id, &username) {
        let new_count = post.like_count.fetch_add(1, Ordering::Relaxed) + 1;
        if new_count == 1 || new_count % 16 == 0 {
            update_trending(&state, &post);
        }
        let _ = state.like_tx.try_send(LikeWrite { post_id, username });
    }

    HttpResponse::Created().json(vec![post.response()])
}

async fn get_feed(state: web::Data<Arc<AppState>>, query: web::Query<FeedQuery>) -> HttpResponse {
    let username = match clean_required(&query.username) {
        Some(v) => v,
        None => return HttpResponse::BadRequest().finish(),
    };

    let response = {
        let mem = state.mem.read().unwrap();
        let mut out = Vec::with_capacity(FEED_LIMIT);
        if let Some(feed) = mem.feeds.get(&username) {
            for post_id in feed.iter().take(FEED_LIMIT) {
                if let Some(post) = mem.posts.get(post_id) {
                    out.push(post.response());
                }
            }
        }
        out
    };
    HttpResponse::Ok().json(response)
}

async fn get_trending(state: web::Data<Arc<AppState>>) -> HttpResponse {
    let trending = state.trending.read().unwrap().clone();
    HttpResponse::Ok().json(trending)
}

async fn search_posts() -> HttpResponse {
    HttpResponse::Ok().json(Vec::<PostResponse>::new())
}

async fn user_profile(state: web::Data<Arc<AppState>>, path: web::Path<String>) -> HttpResponse {
    let username = path.into_inner();
    let response = {
        let mem = state.mem.read().unwrap();
        let user = mem.users.get(&username);
        ProfileResponse {
            username: username.clone(),
            full_name: user
                .map(|u| u.full_name.clone())
                .unwrap_or_else(|| username.clone()),
            bio: user.map(|u| u.bio.clone()).unwrap_or_default(),
            post_count: mem.user_posts.get(&username).map(Vec::len).unwrap_or(0),
            follower_count: mem.followers.get(&username).map(HashSet::len).unwrap_or(0),
            following_count: mem.follows.get(&username).map(HashSet::len).unwrap_or(0),
        }
    };
    HttpResponse::Ok().json(response)
}

async fn notifications() -> HttpResponse {
    HttpResponse::Ok().json(Vec::<serde_json::Value>::new())
}

fn mark_liked(state: &AppState, post_id: i64, username: &str) -> bool {
    let idx = (post_id as usize) & (LIKE_SHARDS - 1);
    let mut shard = state.liked[idx].lock().unwrap();
    shard.insert((post_id, username.to_owned()))
}

fn update_trending(state: &AppState, post: &Post) {
    let _guard = state.trending_lock.lock().unwrap();
    let mut next = state.trending.read().unwrap().clone();
    let mut found = false;
    for item in &mut next {
        if item.id == post.id {
            *item = post.trending_response();
            found = true;
            break;
        }
    }
    if !found {
        next.push(post.trending_response());
    }
    next.sort_by(|a, b| {
        b.like_count
            .cmp(&a.like_count)
            .then_with(|| b.id.cmp(&a.id))
    });
    if next.len() > TRENDING_KEEP {
        next.truncate(TRENDING_KEEP);
    }
    let mut published = next;
    if published.len() > TRENDING_LIMIT {
        published.truncate(TRENDING_LIMIT);
    }
    *state.trending.write().unwrap() = published;
}

fn prepend_limit(v: &mut Vec<i64>, id: i64, limit: usize) {
    if v.contains(&id) {
        return;
    }
    v.insert(0, id);
    if v.len() > limit {
        v.truncate(limit);
    }
}

fn now_rfc3339ish() -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    format!("{}.{:09}Z", now.as_secs(), now.subsec_nanos())
}

async fn user_writer(pool: Pool, mut rx: mpsc::Receiver<UserWrite>) {
    while let Some(item) = rx.recv().await {
        if let Ok(Ok(client)) = time::timeout(WRITE_TIMEOUT, pool.get()).await {
            let _ = time::timeout(
                WRITE_TIMEOUT,
                client.execute(
                    "INSERT INTO users (username, full_name, bio) VALUES ($1, $2, $3) ON CONFLICT (username) DO NOTHING",
                    &[&item.username, &item.full_name, &item.bio],
                ),
            )
            .await;
        }
    }
}

async fn post_writer(pool: Pool, mut rx: mpsc::Receiver<PostWrite>) {
    while let Some(item) = rx.recv().await {
        if let Ok(Ok(client)) = time::timeout(WRITE_TIMEOUT, pool.get()).await {
            let _ = time::timeout(
                WRITE_TIMEOUT,
                client.execute(
                    "INSERT INTO posts (id, username, content, created_at, like_count) VALUES ($1, $2, $3, NOW(), 0) ON CONFLICT (id) DO NOTHING",
                    &[&item.id, &item.username, &item.content],
                ),
            )
            .await;
        }
    }
}

async fn follow_writer(pool: Pool, mut rx: mpsc::Receiver<FollowWrite>) {
    while let Some(item) = rx.recv().await {
        if let Ok(Ok(client)) = time::timeout(WRITE_TIMEOUT, pool.get()).await {
            let _ = time::timeout(
                WRITE_TIMEOUT,
                client.execute(
                    "INSERT INTO follows (follower_username, following_username) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    &[&item.follower, &item.following],
                ),
            )
            .await;
        }
    }
}

async fn like_writer(pool: Pool, mut rx: mpsc::Receiver<LikeWrite>) {
    let mut batch = Vec::with_capacity(LIKE_BATCH_SIZE);
    let timer = time::sleep(LIKE_BATCH_MAX_WAIT);
    tokio::pin!(timer);

    loop {
        tokio::select! {
            item = rx.recv() => {
                match item {
                    Some(item) => {
                        batch.push(item);
                        if batch.len() >= LIKE_BATCH_SIZE {
                            flush_likes(&pool, &mut batch).await;
                            timer.as_mut().reset(Instant::now() + LIKE_BATCH_MAX_WAIT);
                        }
                    }
                    None => break,
                }
            }
            _ = &mut timer => {
                if !batch.is_empty() {
                    flush_likes(&pool, &mut batch).await;
                }
                timer.as_mut().reset(Instant::now() + LIKE_BATCH_MAX_WAIT);
            }
        }
    }
}

async fn flush_likes(pool: &Pool, batch: &mut Vec<LikeWrite>) {
    if batch.is_empty() {
        return;
    }
    let Ok(Ok(client)) = time::timeout(WRITE_TIMEOUT, pool.get()).await else {
        batch.clear();
        return;
    };

    let mut insert_sql = String::from("INSERT INTO post_likes (post_id, username) VALUES ");
    let mut params: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> =
        Vec::with_capacity(batch.len() * 2);
    for (i, item) in batch.iter().enumerate() {
        if i > 0 {
            insert_sql.push(',');
        }
        let _ = write!(insert_sql, "(${},${})", i * 2 + 1, i * 2 + 2);
        params.push(&item.post_id);
        params.push(&item.username);
    }
    insert_sql.push_str(" ON CONFLICT DO NOTHING");
    let _ = time::timeout(WRITE_TIMEOUT, client.execute(insert_sql.as_str(), &params)).await;

    let mut deltas: HashMap<i64, i64> = HashMap::new();
    for item in batch.iter() {
        *deltas.entry(item.post_id).or_default() += 1;
    }
    batch.clear();
    if deltas.is_empty() {
        return;
    }

    let mut update_sql =
        String::from("UPDATE posts AS p SET like_count = p.like_count + v.delta FROM (VALUES ");
    let mut post_ids = Vec::with_capacity(deltas.len());
    let mut counts = Vec::with_capacity(deltas.len());
    for (i, (post_id, count)) in deltas.into_iter().enumerate() {
        if i > 0 {
            update_sql.push(',');
        }
        let _ = write!(
            update_sql,
            "(${}::bigint,${}::bigint)",
            i * 2 + 1,
            i * 2 + 2
        );
        post_ids.push(post_id);
        counts.push(count);
    }
    update_sql.push_str(") AS v(id, delta) WHERE p.id = v.id");

    let mut update_params: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> =
        Vec::with_capacity(post_ids.len() * 2);
    for i in 0..post_ids.len() {
        update_params.push(&post_ids[i]);
        update_params.push(&counts[i]);
    }
    let _ = time::timeout(
        WRITE_TIMEOUT,
        client.execute(update_sql.as_str(), &update_params),
    )
    .await;
}

fn make_pool() -> Pool {
    let mut cfg = Config::new();
    cfg.host = Some(env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_owned()));
    cfg.port = Some(
        env::var("DB_PORT")
            .unwrap_or_else(|_| "5432".to_owned())
            .parse()
            .unwrap_or(5432),
    );
    cfg.user = Some(env::var("DB_USER").unwrap_or_else(|_| "postgres".to_owned()));
    cfg.password = Some(env::var("DB_PASSWORD").unwrap_or_else(|_| "postgres".to_owned()));
    cfg.dbname = Some(env::var("DB_NAME").unwrap_or_else(|_| "testdb".to_owned()));
    cfg.manager = Some(ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    });
    cfg.pool = Some(deadpool_postgres::PoolConfig {
        max_size: 96,
        ..Default::default()
    });
    cfg.create_pool(Some(Runtime::Tokio1), NoTls)
        .expect("create postgres pool")
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let pool = make_pool();
    init_db(&pool).await;

    let (user_tx, user_rx) = mpsc::channel(131_072);
    let (post_tx, post_rx) = mpsc::channel(262_144);
    let (follow_tx, follow_rx) = mpsc::channel(262_144);
    let (like_tx, like_rx) = mpsc::channel(1_048_576);

    let state = Arc::new(AppState {
        mem: RwLock::new(MemState::default()),
        next_post_id: AtomicI64::new(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_micros() as i64,
        ),
        trending: RwLock::new(Vec::new()),
        trending_lock: Mutex::new(()),
        liked: (0..LIKE_SHARDS)
            .map(|_| Mutex::new(HashSet::new()))
            .collect(),
        user_tx,
        post_tx,
        follow_tx,
        like_tx,
    });

    tokio::spawn(user_writer(pool.clone(), user_rx));
    tokio::spawn(post_writer(pool.clone(), post_rx));
    tokio::spawn(follow_writer(pool.clone(), follow_rx));
    tokio::spawn(like_writer(pool.clone(), like_rx));

    let port: u16 = env::var("PORT")
        .unwrap_or_else(|_| "5001".to_owned())
        .parse()
        .unwrap_or(5001);
    let workers = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4);

    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(state.clone()))
            .app_data(
                web::JsonConfig::default()
                    .limit(1 << 20)
                    .error_handler(|err, _| {
                        actix_web::error::InternalError::from_response(
                            err,
                            HttpResponse::BadRequest().finish(),
                        )
                        .into()
                    }),
            )
            .route("/users", web::post().to(create_user))
            .route("/posts", web::post().to(create_post))
            .route("/follow", web::post().to(follow_user))
            .route("/posts/{post_id}/like", web::post().to(like_post))
            .route("/feed", web::get().to(get_feed))
            .route("/trending", web::get().to(get_trending))
            .route("/search", web::get().to(search_posts))
            .route("/notifications", web::get().to(notifications))
            .route("/users/{username}/profile", web::get().to(user_profile))
    })
    .workers(workers)
    .bind(("0.0.0.0", port))?
    .run()
    .await
}
