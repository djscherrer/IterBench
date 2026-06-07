use actix_web::{web, App, HttpServer, HttpResponse};
use chrono::{NaiveDate, Utc};
use deadpool_postgres::{Config, Pool, Runtime};
use serde::{Deserialize, Serialize};
use tokio_postgres::NoTls;
use uuid::Uuid;

#[derive(Serialize)]
struct ClickResponse {
    id: String,
    timestamp: String,
}

#[derive(Deserialize)]
struct ClicksQuery {
    date: String,
    direction: String,
}

async fn init_db(pool: &Pool) {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Warning: Failed to get DB connection for init: {e}. Will retry on first request.");
            return;
        }
    };
    if let Err(e) = client
        .batch_execute(
            "CREATE TABLE IF NOT EXISTS clicks (
                id UUID PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_clicks_timestamp ON clicks (timestamp);",
        )
        .await
    {
        eprintln!("Warning: Failed to create table: {e}");
    }
}

async fn register_click(pool: web::Data<Pool>) -> HttpResponse {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::BadRequest().finish(),
    };
    let id = Uuid::new_v4();
    let now = Utc::now();
    match client
        .execute(
            "INSERT INTO clicks (id, timestamp) VALUES ($1, $2)",
            &[&id, &now],
        )
        .await
    {
        Ok(_) => HttpResponse::Created().finish(),
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

async fn get_clicks(pool: web::Data<Pool>, query: web::Query<ClicksQuery>) -> HttpResponse {
    let date = match NaiveDate::parse_from_str(&query.date, "%Y-%m-%d") {
        Ok(d) => d,
        Err(_) => return HttpResponse::BadRequest().finish(),
    };

    let op = match query.direction.as_str() {
        "<" | ">" | "<=" | ">=" => query.direction.as_str(),
        _ => return HttpResponse::BadRequest().finish(),
    };

    let datetime = date.and_hms_opt(0, 0, 0).unwrap();
    let datetime_utc = datetime.and_utc();

    let sql = format!(
        "SELECT id, timestamp FROM clicks WHERE timestamp {} $1 ORDER BY timestamp",
        op
    );

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::BadRequest().finish(),
    };

    let rows = match client.query(&sql as &str, &[&datetime_utc]).await {
        Ok(r) => r,
        Err(_) => return HttpResponse::BadRequest().finish(),
    };

    if rows.is_empty() {
        return HttpResponse::NotFound().finish();
    }

    let clicks: Vec<ClickResponse> = rows
        .iter()
        .map(|row| {
            let id: Uuid = row.get("id");
            let ts: chrono::DateTime<Utc> = row.get("timestamp");
            ClickResponse {
                id: id.to_string(),
                timestamp: ts.to_rfc3339(),
            }
        })
        .collect();

    HttpResponse::Ok().json(clicks)
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(5001);

    let mut cfg = Config::new();
    cfg.host = Some(std::env::var("DB_HOST").unwrap_or_else(|_| "localhost".into()));
    cfg.port = Some(
        std::env::var("DB_PORT")
            .ok()
            .and_then(|p| p.parse().ok())
            .unwrap_or(5432),
    );
    cfg.user = Some(std::env::var("DB_USER").unwrap_or_else(|_| "postgres".into()));
    cfg.password = Some(std::env::var("DB_PASSWORD").unwrap_or_else(|_| "postgres".into()));
    cfg.dbname = Some(std::env::var("DB_NAME").unwrap_or_else(|_| "testdb".into()));

    let pool = cfg
        .create_pool(Some(Runtime::Tokio1), NoTls)
        .expect("Failed to create pool");

    init_db(&pool).await;

    let pool_data = web::Data::new(pool);

    HttpServer::new(move || {
        App::new()
            .app_data(pool_data.clone())
            .route("/click", web::post().to(register_click))
            .route("/clicks", web::get().to(get_clicks))
    })
    .workers(num_cpus())
    .bind(("0.0.0.0", port))?
    .run()
    .await
}

fn num_cpus() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4)
}
