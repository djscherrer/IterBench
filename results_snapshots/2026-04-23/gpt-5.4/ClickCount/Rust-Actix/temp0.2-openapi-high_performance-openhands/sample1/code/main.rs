use actix_web::http::StatusCode;
use actix_web::{middleware::Logger, web, App, HttpResponse, HttpServer, ResponseError};
use chrono::{DateTime, NaiveDate, Utc};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use serde::{Deserialize, Serialize};
use std::env;
use std::fmt::{Display, Formatter};
use tokio_postgres::NoTls;
use uuid::Uuid;

#[derive(Clone)]
struct AppState {
    pool: Pool,
}

#[derive(Debug)]
enum AppError {
    BadRequest(String),
    NotFound(String),
    Internal(String),
}

impl Display for AppError {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::BadRequest(message) | Self::NotFound(message) | Self::Internal(message) => {
                f.write_str(message)
            }
        }
    }
}

impl ResponseError for AppError {
    fn status_code(&self) -> StatusCode {
        match self {
            Self::BadRequest(_) => StatusCode::BAD_REQUEST,
            Self::NotFound(_) => StatusCode::NOT_FOUND,
            Self::Internal(_) => StatusCode::INTERNAL_SERVER_ERROR,
        }
    }

    fn error_response(&self) -> HttpResponse {
        HttpResponse::build(self.status_code()).body(self.to_string())
    }
}

impl From<deadpool_postgres::PoolError> for AppError {
    fn from(error: deadpool_postgres::PoolError) -> Self {
        Self::Internal(format!("database pool error: {error}"))
    }
}

impl From<tokio_postgres::Error> for AppError {
    fn from(error: tokio_postgres::Error) -> Self {
        Self::Internal(format!("database error: {error}"))
    }
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

async fn register_click(state: web::Data<AppState>) -> Result<HttpResponse, AppError> {
    let client = state.pool.get().await?;
    let id = Uuid::new_v4();
    let timestamp = Utc::now();

    client
        .execute(
            "INSERT INTO clicks (id, timestamp) VALUES ($1, $2)",
            &[&id, &timestamp],
        )
        .await?;

    Ok(HttpResponse::Created().finish())
}

async fn get_clicks(
    state: web::Data<AppState>,
    query: web::Query<ClicksQuery>,
) -> Result<HttpResponse, AppError> {
    let date = NaiveDate::parse_from_str(&query.date, "%Y-%m-%d")
        .map_err(|_| AppError::BadRequest("invalid date, expected YYYY-MM-DD".to_string()))?;
    let boundary = DateTime::<Utc>::from_naive_utc_and_offset(
        date.and_hms_opt(0, 0, 0)
            .ok_or_else(|| AppError::BadRequest("invalid date".to_string()))?,
        Utc,
    );

    let sql = match query.direction.as_str() {
        "<" => "SELECT id, timestamp FROM clicks WHERE timestamp < $1 ORDER BY timestamp DESC",
        "<=" => "SELECT id, timestamp FROM clicks WHERE timestamp <= $1 ORDER BY timestamp DESC",
        ">" => "SELECT id, timestamp FROM clicks WHERE timestamp > $1 ORDER BY timestamp DESC",
        ">=" => "SELECT id, timestamp FROM clicks WHERE timestamp >= $1 ORDER BY timestamp DESC",
        _ => {
            return Err(AppError::BadRequest(
                "invalid direction, expected one of <, <=, >, >=".to_string(),
            ))
        }
    };

    let client = state.pool.get().await?;
    let rows = client.query(sql, &[&boundary]).await?;

    if rows.is_empty() {
        return Err(AppError::NotFound("no clicks found".to_string()));
    }

    let clicks = rows
        .into_iter()
        .map(|row| Click {
            id: row.get::<_, Uuid>("id").to_string(),
            timestamp: row.get("timestamp"),
        })
        .collect::<Vec<_>>();

    Ok(HttpResponse::Ok().json(clicks))
}

fn database_config_from_env() -> Result<tokio_postgres::Config, AppError> {
    let host = env::var("DB_HOST")
        .map_err(|_| AppError::Internal("missing DB_HOST environment variable".to_string()))?;
    let port = env::var("DB_PORT")
        .map_err(|_| AppError::Internal("missing DB_PORT environment variable".to_string()))?
        .parse::<u16>()
        .map_err(|_| AppError::Internal("invalid DB_PORT environment variable".to_string()))?;
    let user = env::var("DB_USER")
        .map_err(|_| AppError::Internal("missing DB_USER environment variable".to_string()))?;
    let password = env::var("DB_PASSWORD").map_err(|_| {
        AppError::Internal("missing DB_PASSWORD environment variable".to_string())
    })?;
    let dbname = env::var("DB_NAME")
        .map_err(|_| AppError::Internal("missing DB_NAME environment variable".to_string()))?;

    let mut config = tokio_postgres::Config::new();
    config.host(&host);
    config.port(port);
    config.user(&user);
    config.password(password);
    config.dbname(&dbname);
    config.application_name("click-tracking-api");
    config
        .connect_timeout(std::time::Duration::from_secs(5));

    Ok(config)
}

async fn create_pool() -> Result<Pool, AppError> {
    let config = database_config_from_env()?;
    let manager = Manager::from_config(
        config,
        NoTls,
        ManagerConfig {
            recycling_method: RecyclingMethod::Fast,
        },
    );

    let max_size = std::thread::available_parallelism()
        .map(|parallelism| (parallelism.get() * 4).clamp(16, 64) as usize)
        .unwrap_or(16);

    Ok(Pool::builder(manager).max_size(max_size).build().map_err(|error| {
        AppError::Internal(format!("failed to create database pool: {error}"))
    })?)
}

async fn initialize_database(pool: &Pool) -> Result<(), AppError> {
    let client = pool.get().await?;
    client
        .batch_execute(
            "
            CREATE TABLE IF NOT EXISTS clicks (
                id UUID PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_clicks_timestamp_desc ON clicks (timestamp DESC);
            ",
        )
        .await?;

    Ok(())
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let pool = create_pool()
        .await
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::Other, error.to_string()))?;
    initialize_database(&pool)
        .await
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::Other, error.to_string()))?;

    let state = web::Data::new(AppState { pool });
    let port = env::var("PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(5001);

    HttpServer::new(move || {
        App::new()
            .app_data(state.clone())
            .wrap(Logger::default())
            .route("/click", web::post().to(register_click))
            .route("/clicks", web::get().to(get_clicks))
    })
    .workers(std::thread::available_parallelism().map(|value| value.get()).unwrap_or(4))
    .bind(("0.0.0.0", port))?
    .run()
    .await
}
