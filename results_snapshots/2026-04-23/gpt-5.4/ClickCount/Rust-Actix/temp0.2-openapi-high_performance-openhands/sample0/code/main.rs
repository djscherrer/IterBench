use std::env;
use std::io;

use actix_web::{
    http::StatusCode,
    middleware::Logger,
    web, App, HttpResponse, HttpServer, ResponseError,
};
use chrono::{DateTime, Days, NaiveDate, Utc};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod, Runtime};
use serde::{Deserialize, Serialize};
use tokio_postgres::{types::ToSql, NoTls};
use uuid::Uuid;

#[derive(Clone)]
struct AppState {
    pool: Pool,
}

#[derive(Debug)]
enum AppError {
    Db(deadpool_postgres::PoolError),
    Sql(tokio_postgres::Error),
    Config(String),
}

impl std::fmt::Display for AppError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Db(err) => write!(f, "database pool error: {err}"),
            Self::Sql(err) => write!(f, "database error: {err}"),
            Self::Config(err) => write!(f, "configuration error: {err}"),
        }
    }
}

impl std::error::Error for AppError {}

impl ResponseError for AppError {
    fn status_code(&self) -> StatusCode {
        match self {
            Self::Config(_) => StatusCode::BAD_REQUEST,
            Self::Db(_) | Self::Sql(_) => StatusCode::INTERNAL_SERVER_ERROR,
        }
    }

    fn error_response(&self) -> HttpResponse {
        HttpResponse::build(self.status_code()).finish()
    }
}

impl From<deadpool_postgres::PoolError> for AppError {
    fn from(value: deadpool_postgres::PoolError) -> Self {
        Self::Db(value)
    }
}

impl From<tokio_postgres::Error> for AppError {
    fn from(value: tokio_postgres::Error) -> Self {
        Self::Sql(value)
    }
}

#[derive(Serialize)]
struct ClickResponse {
    id: String,
    timestamp: DateTime<Utc>,
}

#[derive(Deserialize)]
struct ClickQuery {
    date: NaiveDate,
    direction: Direction,
}

#[derive(Clone, Copy, Deserialize)]
enum Direction {
    #[serde(rename = "<")]
    LessThan,
    #[serde(rename = ">")]
    GreaterThan,
    #[serde(rename = "<=")]
    LessThanOrEqual,
    #[serde(rename = ">=")]
    GreaterThanOrEqual,
}

impl Direction {
    fn predicate(self, date: NaiveDate) -> Result<(&'static str, DateTime<Utc>), AppError> {
        let start = date
            .and_hms_opt(0, 0, 0)
            .ok_or_else(|| AppError::Config("invalid filter date".to_string()))?
            .and_utc();
        let next_day = date
            .checked_add_days(Days::new(1))
            .ok_or_else(|| AppError::Config("date overflow".to_string()))?
            .and_hms_opt(0, 0, 0)
            .ok_or_else(|| AppError::Config("invalid next-day boundary".to_string()))?
            .and_utc();

        Ok(match self {
            Self::LessThan => ("<", start),
            Self::LessThanOrEqual => ("<", next_day),
            Self::GreaterThan => (">=", next_day),
            Self::GreaterThanOrEqual => (">=", start),
        })
    }
}

async fn register_click(state: web::Data<AppState>) -> Result<HttpResponse, AppError> {
    let client = state.pool.get().await?;
    let id = Uuid::new_v4();
    let row = client
        .query_one(
            "INSERT INTO clicks (id) VALUES ($1) RETURNING id, timestamp",
            &[&id],
        )
        .await?;

    let response = ClickResponse {
        id: row.get::<_, Uuid>(0).to_string(),
        timestamp: row.get(1),
    };

    Ok(HttpResponse::Created().json(response))
}

async fn get_clicks(
    state: web::Data<AppState>,
    query: web::Query<ClickQuery>,
) -> Result<HttpResponse, AppError> {
    let (operator, boundary) = query.direction.predicate(query.date)?;
    let sql = match operator {
        "<" => "SELECT id, timestamp FROM clicks WHERE timestamp < $1 ORDER BY timestamp ASC, id ASC",
        ">=" => {
            "SELECT id, timestamp FROM clicks WHERE timestamp >= $1 ORDER BY timestamp ASC, id ASC"
        }
        _ => unreachable!(),
    };

    let client = state.pool.get().await?;
    let stmt = client.prepare_cached(sql).await?;
    let params: [&(dyn ToSql + Sync); 1] = [&boundary];
    let rows = client.query(&stmt, &params).await?;

    if rows.is_empty() {
        return Ok(HttpResponse::NotFound().finish());
    }

    let clicks = rows
        .into_iter()
        .map(|row| ClickResponse {
            id: row.get::<_, Uuid>(0).to_string(),
            timestamp: row.get(1),
        })
        .collect::<Vec<_>>();

    Ok(HttpResponse::Ok().json(clicks))
}

fn required_env(name: &str) -> Result<String, AppError> {
    env::var(name).map_err(|_| AppError::Config(format!("missing environment variable {name}")))
}

fn parse_port() -> Result<u16, AppError> {
    env::var("PORT")
        .unwrap_or_else(|_| "5001".to_string())
        .parse::<u16>()
        .map_err(|err| AppError::Config(format!("invalid PORT: {err}")))
}

fn build_pool() -> Result<Pool, AppError> {
    let mut config = tokio_postgres::Config::new();
    config.host(&required_env("DB_HOST")?);
    config.port(
        required_env("DB_PORT")?
            .parse::<u16>()
            .map_err(|err| AppError::Config(format!("invalid DB_PORT: {err}")))?,
    );
    config.user(&required_env("DB_USER")?);
    config.password(required_env("DB_PASSWORD")?);
    config.dbname(&required_env("DB_NAME")?);
    config.application_name("click-tracking-api");

    let manager = Manager::from_config(
        config,
        NoTls,
        ManagerConfig {
            recycling_method: RecyclingMethod::Fast,
        },
    );

    let worker_hint = std::thread::available_parallelism()
        .map(|value| value.get())
        .unwrap_or(4);
    let max_size = (worker_hint.saturating_mul(4)).clamp(16, 64) as usize;

    Pool::builder(manager)
        .max_size(max_size)
        .runtime(Runtime::Tokio1)
        .build()
        .map_err(|err| AppError::Config(format!("failed to build pool: {err}")))
}

async fn init_db(pool: &Pool) -> Result<(), AppError> {
    let client = pool.get().await?;
    client
        .batch_execute(
            "
            CREATE TABLE IF NOT EXISTS clicks (
                id UUID PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_clicks_timestamp_id ON clicks (timestamp, id);
            ",
        )
        .await?;
    Ok(())
}

#[actix_web::main]
async fn main() -> io::Result<()> {
    env_logger::init();

    let pool = build_pool().map_err(io::Error::other)?;
    init_db(&pool).await.map_err(io::Error::other)?;

    let state = web::Data::new(AppState { pool });
    let port = parse_port().map_err(io::Error::other)?;
    let workers = std::thread::available_parallelism()
        .map(|value| value.get())
        .unwrap_or(4);

    HttpServer::new(move || {
        App::new()
            .app_data(state.clone())
            .wrap(Logger::default())
            .route("/click", web::post().to(register_click))
            .route("/clicks", web::get().to(get_clicks))
    })
    .workers(workers)
    .bind(("0.0.0.0", port))?
    .run()
    .await
}
