use actix_web::{web, App, HttpResponse, HttpServer, Responder};
use chrono::{DateTime, NaiveDate, NaiveDateTime, Utc};
use deadpool_postgres::{Config, ManagerConfig, Pool, RecyclingMethod, Runtime};
use serde::{Deserialize, Serialize};
use std::env;
use tokio_postgres::NoTls;

#[derive(Serialize)]
struct ClickRecord {
    id: String,
    timestamp: DateTime<Utc>,
}

#[derive(Deserialize)]
struct ClicksQuery {
    date: String,
    direction: String,
}

async fn register_click(pool: web::Data<Pool>) -> impl Responder {
    let client = match pool.get().await {
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
        Ok(_) => HttpResponse::Created().finish(),
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

async fn get_clicks(
    pool: web::Data<Pool>,
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

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let query_str = format!(
        "SELECT id, timestamp FROM clicks WHERE timestamp {} $1",
        direction
    );

    let rows = match client.query(query_str.as_str(), &[&datetime]).await {
        Ok(r) => r,
        Err(_) => return HttpResponse::BadRequest().finish(),
    };

    if rows.is_empty() {
        return HttpResponse::NotFound().finish();
    }

    let result: Vec<ClickRecord> = rows
        .iter()
        .map(|row| {
            let id: uuid::Uuid = row.get(0);
            let ts: DateTime<Utc> = row.get(1);
            ClickRecord {
                id: id.to_string(),
                timestamp: ts,
            }
        })
        .collect();

    HttpResponse::Ok().json(result)
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
