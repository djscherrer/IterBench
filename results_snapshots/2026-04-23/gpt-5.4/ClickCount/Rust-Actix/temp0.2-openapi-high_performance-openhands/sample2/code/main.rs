use std::env;
use std::io;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::time::Duration;

use actix_web::{
    error::{ErrorBadRequest, ErrorInternalServerError, ErrorNotFound},
    middleware::Logger,
    web, App, HttpResponse, HttpServer, Result,
};
use chrono::{DateTime, NaiveDate, Utc};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use serde::{Deserialize, Serialize};
use tokio_postgres::{Config, NoTls};
use uuid::Uuid;

#[derive(Clone)]
struct AppState {
    pool: Pool,
    initialized: Arc<AtomicBool>,
}

#[derive(Deserialize)]
struct ClicksQuery {
    date: String,
    direction: String,
}

#[derive(Serialize)]
struct ClickResponse {
    id: String,

async fn ensure_database_ready(state: &AppState) -> io::Result<()> {
    if state.initialized.load(Ordering::Acquire) {
        return Ok(());
    }

    initialize_database(&state.pool).await?;
    state.initialized.store(true, Ordering::Release);
    Ok(())
}

    timestamp: DateTime<Utc>,
}

async fn create_click(state: web::Data<AppState>) -> Result<HttpResponse> {
    let client = state
        .pool
        .get()
        .await
        .map_err(ErrorInternalServerError)?;

    let click_id = Uuid::new_v4();
    let timestamp = Utc::now();

    client
        .execute(
            "INSERT INTO clicks (id, timestamp) VALUES ($1, $2)",
            &[&click_id, &timestamp],
        )
        .await
        .map_err(ErrorInternalServerError)?;

    Ok(HttpResponse::Created().finish())
}

async fn get_clicks(
    state: web::Data<AppState>,
    query: web::Query<ClicksQuery>,
) -> Result<HttpResponse> {
    let date = NaiveDate::parse_from_str(&query.date, "%Y-%m-%d")
        .map_err(|_| ErrorBadRequest("invalid date, expected YYYY-MM-DD"))?;
    let boundary = date
        .and_hms_opt(0, 0, 0)
        .ok_or_else(|| ErrorBadRequest("invalid date"))?
        .and_utc();

    let sql = match query.direction.as_str() {
        "<" => "SELECT id, timestamp FROM clicks WHERE timestamp < $1 ORDER BY timestamp ASC",
        "<=" => "SELECT id, timestamp FROM clicks WHERE timestamp <= $1 ORDER BY timestamp ASC",
        ">" => "SELECT id, timestamp FROM clicks WHERE timestamp > $1 ORDER BY timestamp ASC",
        ">=" => "SELECT id, timestamp FROM clicks WHERE timestamp >= $1 ORDER BY timestamp ASC",
        _ => return Err(ErrorBadRequest("invalid direction, expected one of <, <=, >, >=")),
    };

    let client = state
        .pool
        .get()
        .await
        .map_err(ErrorInternalServerError)?;
    let rows = client
        .query(sql, &[&boundary])
        .await
        .map_err(ErrorInternalServerError)?;

    if rows.is_empty() {
        return Err(ErrorNotFound("no clicks found"));
    }

    let clicks = rows
        .into_iter()
        .map(|row| ClickResponse {
            id: row.get::<_, Uuid>("id").to_string(),
            timestamp: row.get("timestamp"),
        })
        .collect::<Vec<_>>();

    Ok(HttpResponse::Ok().json(clicks))
}

fn database_config_from_env() -> Result<Config, io::Error> {
    let mut config = Config::new();
    config.host(
        &env::var("DB_HOST").map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))?,
    );
    config.port(
        env::var("DB_PORT")
            .ok()
            .and_then(|value| value.parse::<u16>().ok())
            .unwrap_or(5432),
    );
    config.user(
        &env::var("DB_USER").map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))?,
    );
    config.password(
        &env::var("DB_PASSWORD").map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))?,
    );
    config.dbname(
        &env::var("DB_NAME").map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))?,
    );
    Ok(config)
}

fn create_pool(config: Config) -> Pool {
    let manager = Manager::from_config(
        config,
        NoTls,
        ManagerConfig {
            recycling_method: RecyclingMethod::Fast,
        },
    );

    Pool::builder(manager)
        .max_size(32)
        .build()
        .expect("failed to build PostgreSQL pool")
}

async fn initialize_database(pool: &Pool) -> io::Result<()> {
    let client = pool
        .get()
        .await
        .map_err(|err| io::Error::new(io::ErrorKind::Other, err.to_string()))?;

    client
        .batch_execute(
            "
            CREATE TABLE IF NOT EXISTS clicks (
                id UUID PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_clicks_timestamp ON clicks (timestamp);
            ",
        )
        .await
        .map_err(|err| io::Error::new(io::ErrorKind::Other, err.to_string()))
}

#[actix_web::main]
async fn main() -> io::Result<()> {
    env_logger::init();

    let config = database_config_from_env()?;
    let pool = create_pool(config);
    initialize_database(&pool).await?;

    let state = AppState { pool };
    let port = env::var("PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(5001);

    HttpServer::new(move || {
        App::new()
            .wrap(Logger::default())
            .app_data(web::Data::new(state.clone()))
            .route("/click", web::post().to(create_click))
            .route("/clicks", web::get().to(get_clicks))
    })
    .bind(("0.0.0.0", port))?
    .workers(std::thread::available_parallelism().map_or(1, usize::from))
    .run()
    .await
}
