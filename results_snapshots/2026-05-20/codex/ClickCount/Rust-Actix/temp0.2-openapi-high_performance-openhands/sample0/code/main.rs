use actix_web::{web, App, HttpResponse, HttpServer, Responder};
use chrono::{DateTime, NaiveDate, NaiveDateTime, Utc};
use deadpool_postgres::{Config, ManagerConfig, Pool, RecyclingMethod, Runtime};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::env;
use std::sync::RwLock;
use tokio_postgres::NoTls;

#[derive(Clone, Serialize)]
struct ClickRecord {
    id: String,
    timestamp: DateTime<Utc>,
}

struct AppState {
    pool: Pool,
    clicks: RwLock<Vec<ClickRecord>>,
    cache: RwLock<HashMap<String, String>>,
}

#[derive(Deserialize)]
struct ClicksQuery {
    date: String,
    direction: String,
}

async fn register_click(state: web::Data<AppState>) -> impl Responder {
    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let id = uuid::Uuid::new_v4();
    let now = Utc::now();

    let res = client
        .execute(
            "INSERT INTO clicks (id, timestamp) VALUES ($1, $2)",
            &[&id, &now],
        )
        .await;

    match res {
        Ok(_) => {
            let record = ClickRecord {
                id: id.to_string(),
                timestamp: now,
            };
            if let Ok(mut clicks) = state.clicks.write() {
                clicks.push(record);
            }
            if let Ok(mut cache) = state.cache.write() {
                cache.clear();
            }
            HttpResponse::Created().finish()
        }
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

async fn get_clicks(
    state: web::Data<AppState>,
    query: web::Query<ClicksQuery>,
) -> impl Responder {
    let direction = query.direction.as_str();
    if !matches!(direction, "<" | "<=" | ">" | ">=") {
        return HttpResponse::BadRequest().finish();
    }

    let date = match NaiveDate::parse_from_str(&query.date, "%Y-%m-%d") {
        Ok(d) => d,
        Err(_) => return HttpResponse::BadRequest().finish(),
    };

    let datetime: DateTime<Utc> =
        DateTime::<Utc>::from_naive_utc_and_offset(
            NaiveDateTime::new(date, chrono::NaiveTime::from_hms_opt(0, 0, 0).unwrap()),
            Utc,
        );

    let cache_key = format!("{}:{}", direction, datetime.to_rfc3339());
    if let Ok(cache) = state.cache.read() {
        if let Some(body) = cache.get(&cache_key) {
            return HttpResponse::Ok()
                .content_type("application/json")
                .body(body.clone());
        }
    }

    let result: Vec<ClickRecord> = match state.clicks.read() {
        Ok(clicks) => clicks
            .iter()
            .filter(|click| matches_direction(click.timestamp, datetime, direction))
            .cloned()
            .collect(),
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    if result.is_empty() {
        return HttpResponse::NotFound().finish();
    }

    let body = match serde_json::to_string(&result) {
        Ok(body) => body,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    if let Ok(mut cache) = state.cache.write() {
        cache.insert(cache_key, body.clone());
    }

    HttpResponse::Ok()
        .content_type("application/json")
        .body(body)
}

fn matches_direction(value: DateTime<Utc>, cutoff: DateTime<Utc>, direction: &str) -> bool {
    match direction {
        "<" => value < cutoff,
        "<=" => value <= cutoff,
        ">" => value > cutoff,
        ">=" => value >= cutoff,
        _ => false,
    }
}

async fn init_db(pool: &Pool) -> Result<(), Box<dyn std::error::Error>> {
    let client = pool.get().await?;
    client
        .batch_execute(
            "CREATE TABLE IF NOT EXISTS clicks (
                id UUID PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_clicks_timestamp ON clicks(timestamp);",
        )
        .await?;
    Ok(())
}

async fn load_clicks(pool: &Pool) -> Result<Vec<ClickRecord>, Box<dyn std::error::Error>> {
    let client = pool.get().await?;
    let rows = client
        .query(
            "SELECT id, timestamp FROM clicks ORDER BY timestamp ASC, id ASC",
            &[],
        )
        .await?;
    let mut clicks = Vec::with_capacity(rows.len());
    for row in rows {
        let id: uuid::Uuid = row.get(0);
        let timestamp: DateTime<Utc> = row.get(1);
        clicks.push(ClickRecord {
            id: id.to_string(),
            timestamp,
        });
    }
    Ok(clicks)
}

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
    cfg.manager = Some(ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    });

    let pool = cfg
        .create_pool(Some(Runtime::Tokio1), NoTls)
        .expect("Failed to create pool");

    if let Err(e) = init_db(&pool).await {
        eprintln!("Failed to initialize database: {}", e);
    }

    let initial_clicks = match load_clicks(&pool).await {
        Ok(clicks) => clicks,
        Err(e) => {
            eprintln!("Failed to load existing clicks: {}", e);
            Vec::new()
        }
    };
    let state = web::Data::new(AppState {
        pool,
        clicks: RwLock::new(initial_clicks),
        cache: RwLock::new(HashMap::new()),
    });

    HttpServer::new(move || {
        App::new()
            .app_data(state.clone())
            .route("/click", web::post().to(register_click))
            .route("/clicks", web::get().to(get_clicks))
    })
    .bind(("0.0.0.0", port))?
    .run()
    .await
}
