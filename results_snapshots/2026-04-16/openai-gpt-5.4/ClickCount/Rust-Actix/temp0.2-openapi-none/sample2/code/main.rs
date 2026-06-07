use actix_web::middleware::Logger;
use actix_web::{web, App, HttpResponse, HttpServer, Responder};
use chrono::{DateTime, NaiveDate, Utc};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use serde::{Deserialize, Serialize};
use std::env;
use std::io;
use tokio_postgres::{Config as PgConfig, NoTls};
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

    let result = client
        .execute(
            "INSERT INTO clicks (id, timestamp) VALUES ($1, $2)",
            &[&id, &timestamp],
        )
        .await;

    match result {
        Ok(_) => HttpResponse::Created().finish(),
        Err(_) => HttpResponse::InternalServerError().body("Failed to register click"),
    }
}

async fn get_clicks(
    state: web::Data<AppState>,
    query: web::Query<ClickQuery>,
) -> impl Responder {
    let date = match NaiveDate::parse_from_str(&query.date, "%Y-%m-%d") {
        Ok(d) => d,
        Err(_) => return HttpResponse::BadRequest().body("Invalid date format, expected YYYY-MM-DD"),
    };

    let operator = match query.direction.as_str() {
        "<" => "<",
        ">" => ">",
        "<=" => "<=",
        ">=" => ">=",
        _ => return HttpResponse::BadRequest().body("Invalid direction, expected one of: <, >, <=, >="),
    };

    let naive_dt = match date.and_hms_opt(0, 0, 0) {
        Some(dt) => dt,
        None => return HttpResponse::BadRequest().body("Invalid date"),
    };
    let filter_ts = DateTime::<Utc>::from_naive_utc_and_offset(naive_dt, Utc);

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

    let rows = match client.query(&sql, &[&filter_ts]).await {
        Ok(rows) => rows,
        Err(_) => return HttpResponse::InternalServerError().body("Failed to query clicks"),
    };

    if rows.is_empty() {
        return HttpResponse::NotFound().body("No clicks found");
    }

    let clicks: Vec<Click> = rows
        .into_iter()
        .map(|row| Click {
            id: row.get::<_, String>("id"),
            timestamp: row.get::<_, DateTime<Utc>>("timestamp"),
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

fn build_pool_from_env() -> Result<Pool, Box<dyn std::error::Error + Send + Sync>> {
    let db_host = env::var("DB_HOST")?;
    let db_port: u16 = env::var("DB_PORT")?.parse()?;
    let db_user = env::var("DB_USER")?;
    let db_password = env::var("DB_PASSWORD")?;
    let db_name = env::var("DB_NAME")?;

    let mut cfg = PgConfig::new();
    cfg.host(&db_host);
    cfg.port(db_port);
    cfg.user(&db_user);
    cfg.password(&db_password);
    cfg.dbname(&db_name);

    let mgr_config = ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    };
    let mgr = Manager::from_config(cfg, NoTls, mgr_config);
    let pool = Pool::builder(mgr).max_size(16).build()?;

    Ok(pool)
}

#[actix_web::main]
async fn main() -> io::Result<()> {
    env_logger::init();

    let pool = build_pool_from_env().map_err(|e| io::Error::new(io::ErrorKind::Other, e.to_string()))?;

    init_db(&pool)
        .await
        .map_err(|e| io::Error::new(io::ErrorKind::Other, format!("Database initialization failed: {}", e)))?;

    let state = AppState { pool };

    let port: u16 = env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
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