use actix_web::{web, App, HttpResponse, HttpServer, Responder, Result};
use chrono::{DateTime, Utc};
use deadpool_postgres::{Config, ManagerConfig, Pool, RecyclingMethod, Runtime};
use serde::{Deserialize, Serialize};
use std::env;
use uuid::Uuid;

#[derive(Serialize, Deserialize, Debug)]
struct Click {
    id: Uuid,
    timestamp: DateTime<Utc>,
}

#[derive(Deserialize)]
struct ClickQuery {
    date: String,
    direction: String,
}

async fn register_click(pool: web::Data<Pool>) -> Result<impl Responder> {
    let client = pool.get().await.map_err(|e| {
        actix_web::error::ErrorInternalServerError(format!("Failed to get DB client: {}", e))
    })?;

    let id = Uuid::new_v4();
    let timestamp = Utc::now();

    client
        .execute(
            "INSERT INTO clicks (id, timestamp) VALUES ($1, $2)",
            &[&id, &timestamp],
        )
        .await
        .map_err(|e| {
            actix_web::error::ErrorInternalServerError(format!("Failed to insert click: {}", e))
        })?;

    Ok(HttpResponse::Created().json(Click { id, timestamp }))
}

async fn get_clicks(
    pool: web::Data<Pool>,
    query: web::Query<ClickQuery>,
) -> Result<impl Responder> {
    let date = chrono::NaiveDate::parse_from_str(&query.date, "%Y-%m-%d").map_err(|e| {
        actix_web::error::ErrorBadRequest(format!("Invalid date format: {}", e))
    })?;

    let datetime = date.and_hms_opt(0, 0, 0).unwrap();
    let timestamp = DateTime::<Utc>::from_naive_utc_and_offset(datetime, Utc);

    let valid_directions = ["<", ">", "<=", ">="];
    if !valid_directions.contains(&query.direction.as_str()) {
        return Err(actix_web::error::ErrorBadRequest(
            "Invalid direction parameter",
        ));
    }

    let sql = format!(
        "SELECT id, timestamp FROM clicks WHERE timestamp {} $1 ORDER BY timestamp",
        query.direction
    );

    let client = pool.get().await.map_err(|e| {
        actix_web::error::ErrorInternalServerError(format!("Failed to get DB client: {}", e))
    })?;

    let rows = client
        .query(&sql, &[&timestamp])
        .await
        .map_err(|e| {
            actix_web::error::ErrorInternalServerError(format!("Failed to query clicks: {}", e))
        })?;

    if rows.is_empty() {
        return Err(actix_web::error::ErrorNotFound("No clicks found"));
    }

    let clicks: Vec<Click> = rows
        .iter()
        .map(|row| Click {
            id: row.get(0),
            timestamp: row.get(1),
        })
        .collect();

    Ok(HttpResponse::Ok().json(clicks))
}

async fn create_table(pool: &Pool) -> std::result::Result<(), Box<dyn std::error::Error>> {
    let client = pool.get().await?;

    client
        .execute(
            "CREATE TABLE IF NOT EXISTS clicks (
                id UUID PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL
            )",
            &[],
        )
        .await?;

    Ok(())
}

fn create_pool() -> std::result::Result<Pool, Box<dyn std::error::Error>> {
    let mut cfg = Config::new();

    cfg.host = Some(
        env::var("DB_HOST")
            .unwrap_or_else(|_| "localhost".to_string())
            .to_string(),
    );
    cfg.port = Some(
        env::var("DB_PORT")
            .unwrap_or_else(|_| "5432".to_string())
            .parse()
            .unwrap_or(5432),
    );
    cfg.user = Some(
        env::var("DB_USER")
            .unwrap_or_else(|_| "postgres".to_string())
            .to_string(),
    );
    cfg.password = Some(
        env::var("DB_PASSWORD")
            .unwrap_or_else(|_| "password".to_string())
            .to_string(),
    );
    cfg.dbname = Some(
        env::var("DB_NAME")
            .unwrap_or_else(|_| "clickdb".to_string())
            .to_string(),
    );

    cfg.manager = Some(ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    });

    let pool = cfg.create_pool(Some(Runtime::Tokio1), tokio_postgres::NoTls)?;
    Ok(pool)
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init_from_env(env_logger::Env::new().default_filter_or("info"));

    let pool = create_pool().map_err(|e| {
        std::io::Error::new(
            std::io::ErrorKind::Other,
            format!("Failed to create database pool: {}", e),
        )
    })?;

    create_table(&pool).await.map_err(|e| {
        std::io::Error::new(
            std::io::ErrorKind::Other,
            format!("Failed to create table: {}", e),
        )
    })?;

    let port = env::var("PORT")
        .unwrap_or_else(|_| "5001".to_string())
        .parse()
        .unwrap_or(5001);

    let server = HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(pool.clone()))
            .route("/click", web::post().to(register_click))
            .route("/clicks", web::get().to(get_clicks))
    })
    .bind(("0.0.0.0", port))?
    .run();

    println!("Server running on 0.0.0.0:{}", port);
    server.await
}