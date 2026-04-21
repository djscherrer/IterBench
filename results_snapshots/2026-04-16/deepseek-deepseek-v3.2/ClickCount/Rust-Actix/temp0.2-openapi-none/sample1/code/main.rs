use actix_web::{web, App, HttpResponse, HttpServer, Responder, Result};
use chrono::{DateTime, NaiveDate, Utc};
use deadpool_postgres::{Config, ManagerConfig, Pool, RecyclingMethod, Runtime};
use serde::{Deserialize, Serialize};
use std::env;
use uuid::Uuid;

#[derive(Serialize, Deserialize)]
struct Click {
    id: String,
    timestamp: DateTime<Utc>,
}

#[derive(Deserialize)]
struct ClickQuery {
    date: NaiveDate,
    direction: String,
}

async fn register_click(pool: web::Data<Pool>) -> Result<impl Responder> {
    let client = pool.get().await.map_err(|e| {
        actix_web::error::ErrorInternalServerError(format!("Database connection error: {}", e))
    })?;

    let id = Uuid::new_v4().to_string();
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

    Ok(HttpResponse::Created().json(Click {
        id,
        timestamp,
    }))
}

async fn get_clicks(
    pool: web::Data<Pool>,
    query: web::Query<ClickQuery>,
) -> Result<impl Responder> {
    let client = pool.get().await.map_err(|e| {
        actix_web::error::ErrorInternalServerError(format!("Database connection error: {}", e))
    })?;

    let date_time = query.date.and_hms_opt(0, 0, 0).unwrap();
    let operator = match query.direction.as_str() {
        "<" => "<",
        ">" => ">",
        "<=" => "<=",
        ">=" => ">=",
        _ => return Err(actix_web::error::ErrorBadRequest("Invalid direction parameter")),
    };

    let query_str = format!("SELECT id, timestamp FROM clicks WHERE timestamp {} $1 ORDER BY timestamp", operator);
    
    let rows = client
        .query(&query_str, &[&date_time])
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
            id: row.get("id"),
            timestamp: row.get("timestamp"),
        })
        .collect();

    Ok(HttpResponse::Ok().json(clicks))
}

async fn initialize_database(pool: &Pool) -> std::result::Result<(), Box<dyn std::error::Error>> {
    let client = pool.get().await?;

    client
        .execute(
            "CREATE TABLE IF NOT EXISTS clicks (
                id VARCHAR(36) PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL
            )",
            &[],
        )
        .await?;

    Ok(())
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let port = env::var("PORT")
        .unwrap_or_else(|_| "5001".to_string())
        .parse::<u16>()
        .unwrap_or(5001);

    let mut cfg = Config::new();
    cfg.host = Some(
        env::var("DB_HOST")
            .unwrap_or_else(|_| "localhost".to_string()),
    );
    cfg.port = Some(
        env::var("DB_PORT")
            .unwrap_or_else(|_| "5432".to_string())
            .parse()
            .unwrap_or(5432),
    );
    cfg.user = Some(
        env::var("DB_USER")
            .unwrap_or_else(|_| "postgres".to_string()),
    );
    cfg.password = Some(
        env::var("DB_PASSWORD")
            .unwrap_or_else(|_| "".to_string()),
    );
    cfg.dbname = Some(
        env::var("DB_NAME")
            .unwrap_or_else(|_| "clickdb".to_string()),
    );
    cfg.manager = Some(ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    });

    let pool = cfg
        .create_pool(Some(Runtime::Tokio1), tokio_postgres::NoTls)
        .expect("Failed to create database pool");

    initialize_database(&pool)
        .await
        .expect("Failed to initialize database");

    println!("Server starting on 0.0.0.0:{}", port);

    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(pool.clone()))
            .route("/click", web::post().to(register_click))
            .route("/clicks", web::get().to(get_clicks))
    })
    .bind(("0.0.0.0", port))?
    .run()
    .await
}