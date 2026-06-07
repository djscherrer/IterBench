use actix_web::error::{ErrorBadRequest, ErrorInternalServerError};
use actix_web::{web, App, HttpResponse, HttpServer, Responder};
use chrono::{DateTime, Duration, NaiveDate, Utc};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod, Runtime};
use serde::{Deserialize, Serialize};
use std::env;
use std::io;
use tokio_postgres::{Config as PgConfig, NoTls};
use uuid::Uuid;

#[derive(Clone)]
struct AppState {
    pool: Pool,
}

#[derive(Deserialize)]
struct ClicksQuery {
    date: String,
    direction: String,
}

#[derive(Serialize)]
struct Click {
    id: String,
    timestamp: DateTime<Utc>,
}

fn env_var(name: &str, default: Option<&str>) -> io::Result<String> {
    match env::var(name) {
        Ok(value) if !value.trim().is_empty() => Ok(value),
        _ => default.map(str::to_owned).ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("missing required environment variable {name}"),
            )
        }),
    }
}

fn build_pool() -> io::Result<Pool> {
    let mut pg_config = PgConfig::new();
    pg_config.host(&env_var("DB_HOST", None)?);
    pg_config.port(
        env_var("DB_PORT", Some("5432"))?
            .parse::<u16>()
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))?,
    );
    pg_config.user(&env_var("DB_USER", None)?);
    pg_config.password(env_var("DB_PASSWORD", Some(""))?);
    pg_config.dbname(&env_var("DB_NAME", None)?);

    let manager_config = ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    };
    let manager = Manager::from_config(pg_config, NoTls, manager_config);
    Pool::builder(manager)
        .max_size(64)
        .runtime(Runtime::Tokio1)
        .build()
        .map_err(|e| io::Error::new(io::ErrorKind::Other, e))
}

async fn init_db(pool: &Pool) -> io::Result<()> {
    let client = pool
        .get()
        .await
        .map_err(|e| io::Error::new(io::ErrorKind::Other, e))?;

    client
        .batch_execute(
            "CREATE TABLE IF NOT EXISTS clicks (
                id UUID PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_clicks_timestamp ON clicks (timestamp);",
        )
        .await
        .map_err(|e| io::Error::new(io::ErrorKind::Other, e))
}

async fn register_click(state: web::Data<AppState>) -> actix_web::Result<impl Responder> {
    let client = state.pool.get().await.map_err(ErrorInternalServerError)?;
    let id = Uuid::new_v4();

    client
        .execute("INSERT INTO clicks (id) VALUES ($1)", &[&id])
        .await
        .map_err(ErrorInternalServerError)?;

    Ok(HttpResponse::Created().finish())
}

fn date_start(date: NaiveDate) -> actix_web::Result<DateTime<Utc>> {
    let naive = date
        .and_hms_opt(0, 0, 0)
        .ok_or_else(|| ErrorBadRequest("invalid date"))?;
    Ok(DateTime::<Utc>::from_naive_utc_and_offset(naive, Utc))
}

fn next_date_start(date: NaiveDate) -> actix_web::Result<DateTime<Utc>> {
    let next = date
        .checked_add_signed(Duration::days(1))
        .ok_or_else(|| ErrorBadRequest("date out of range"))?;
    date_start(next)
}

async fn get_clicks(
    state: web::Data<AppState>,
    query: web::Query<ClicksQuery>,
) -> actix_web::Result<impl Responder> {
    let date = NaiveDate::parse_from_str(&query.date, "%Y-%m-%d")
        .map_err(|_| ErrorBadRequest("date must use YYYY-MM-DD format"))?;

    let start = date_start(date)?;
    let next_start = next_date_start(date)?;

    let (sql, boundary) = match query.direction.as_str() {
        "<" => (
            "SELECT id::text, timestamp FROM clicks WHERE timestamp < $1 ORDER BY timestamp ASC",
            start,
        ),
        "<=" => (
            "SELECT id::text, timestamp FROM clicks WHERE timestamp < $1 ORDER BY timestamp ASC",
            next_start,
        ),
        ">" => (
            "SELECT id::text, timestamp FROM clicks WHERE timestamp >= $1 ORDER BY timestamp ASC",
            next_start,
        ),
        ">=" => (
            "SELECT id::text, timestamp FROM clicks WHERE timestamp >= $1 ORDER BY timestamp ASC",
            start,
        ),
        _ => return Err(ErrorBadRequest("direction must be one of <, <=, >, >=")),
    };

    let client = state.pool.get().await.map_err(ErrorInternalServerError)?;
    let rows = client
        .query(sql, &[&boundary])
        .await
        .map_err(ErrorInternalServerError)?;

    if rows.is_empty() {
        return Ok(HttpResponse::NotFound().finish());
    }

    let clicks: Vec<Click> = rows
        .into_iter()
        .map(|row| Click {
            id: row.get(0),
            timestamp: row.get(1),
        })
        .collect();

    Ok(HttpResponse::Ok().json(clicks))
}

#[actix_web::main]
async fn main() -> io::Result<()> {
    env_logger::init();

    let port = env_var("PORT", Some("5001"))?
        .parse::<u16>()
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))?;

    let pool = build_pool()?;
    init_db(&pool).await?;

    let state = web::Data::new(AppState { pool });
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
