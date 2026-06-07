use actix_web::middleware::Logger;
use actix_web::{web, App, HttpResponse, HttpServer, Responder};
use chrono::{DateTime, NaiveDate, Utc};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use serde::{Deserialize, Serialize};
use std::env;
use std::io;
use tokio_postgres::{Config, NoTls};
use uuid::Uuid;

#[derive(Clone)]
struct AppState {
    pool: Pool,
}

#[derive(Serialize)]
struct Click {
    id: String,
    timestamp: DateTime<Utc>,
}

#[derive(Deserialize)]
struct ClickQuery {
    date: String,
    direction: String,
}

async fn post_click(state: web::Data<AppState>) -> impl Responder {
    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(_) => {
            return HttpResponse::InternalServerError().body("Failed to get database connection");
        }
    };

    let id = Uuid::new_v4().to_string();
    let timestamp = Utc::now();

    match client
        .execute(
            "INSERT INTO clicks (id, timestamp) VALUES ($1, $2)",
            &[&id, &timestamp],
        )
        .await
    {
        Ok(_) => HttpResponse::Created().finish(),
        Err(_) => HttpResponse::InternalServerError().body("Failed to register click"),
    }
}

async fn get_clicks(
    state: web::Data<AppState>,
    query: web::Query<ClickQuery>,
) -> impl Responder {
    let parsed_date = match NaiveDate::parse_from_str(&query.date, "%Y-%m-%d") {
        Ok(date) => date,
        Err(_) => return HttpResponse::BadRequest().body("Invalid date format, expected YYYY-MM-DD"),
    };

    let operator = match query.direction.as_str() {
        "<" | ">" | "<=" | ">=" => query.direction.as_str(),
        _ => return HttpResponse::BadRequest().body("Invalid direction"),
    };

    let filter_timestamp = match parsed_date.and_hms_opt(0, 0, 0) {
        Some(dt) => DateTime::<Utc>::from_naive_utc_and_offset(dt, Utc),
        None => return HttpResponse::BadRequest().body("Invalid date"),
    };

    let sql = format!(
        "SELECT id, timestamp FROM clicks WHERE timestamp {} $1 ORDER BY timestamp ASC",
        operator
    );

    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(_) => {
            return HttpResponse::InternalServerError().body("Failed to get database connection");
        }
    };

    let rows = match client.query(&sql, &[&filter_timestamp]).await {
        Ok(rows) => rows,
        Err(_) => return HttpResponse::InternalServerError().body("Failed to retrieve clicks"),
    };

    if rows.is_empty() {
        return HttpResponse::NotFound().body("No clicks found");
    }

    let clicks: Vec<Click> = rows
        .into_iter()
        .map(|row| Click {
            id: row.get::<_, String>(0),
            timestamp: row.get::<_, DateTime<Utc>>(1),
        })
        .collect();

    HttpResponse::Ok().json(clicks)
}

async fn init_db(pool: &Pool) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let client = pool.get().await?;

    client
        .batch_execute(
            r#"
            CREATE TABLE IF NOT EXISTS clicks (
                id TEXT PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_clicks_timestamp ON clicks (timestamp);
            "#,
        )
        .await?;

    Ok(())
}

fn build_pg_config_from_env() -> Result<Config, io::Error> {
    let host = env::var("DB_HOST")
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "DB_HOST is not set"))?;
    let port = env::var("DB_PORT")
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "DB_PORT is not set"))?
        .parse::<u16>()
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "DB_PORT must be a valid u16"))?;
    let user = env::var("DB_USER")
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "DB_USER is not set"))?;
    let password = env::var("DB_PASSWORD")
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "DB_PASSWORD is not set"))?;
    let dbname = env::var("DB_NAME")
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "DB_NAME is not set"))?;

    let mut cfg = Config::new();
    cfg.host(&host);
    cfg.port(port);
    cfg.user(&user);
    cfg.password(&password);
    cfg.dbname(&dbname);

    Ok(cfg)
}

fn build_pool(pg_config: Config) -> Pool {
    let mgr_config = ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    };
    let manager = Manager::from_config(pg_config, NoTls, mgr_config);
    Pool::builder(manager).max_size(16).build().unwrap()
}

#[actix_web::main]
async fn main() -> io::Result<()> {
    env_logger::init();

    let pg_config = build_pg_config_from_env()?;
    let pool = build_pool(pg_config);

    init_db(&pool)
        .await
        .map_err(|e| io::Error::new(io::ErrorKind::Other, format!("Database initialization failed: {e}")))?;

    let state = AppState { pool };

    let port = env::var("PORT")
        .ok()
        .and_then(|v| v.parse::<u16>().ok())
        .unwrap_or(5001);

    HttpServer::new(move || {
        App::new()
            .wrap(Logger::default())
            .app_data(web::Data::new(state.clone()))
            .route("/click", web::post().to(post_click))
            .route("/clicks", web::get().to(get_clicks))
    })
    .bind(("0.0.0.0", port))?
    .run()
    .await
}