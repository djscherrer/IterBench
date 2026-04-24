use actix_web::{web, App, HttpServer, HttpResponse, get, post};
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
    let client = pool.get().await.expect("Failed to get DB client");
    client
        .batch_execute(
            "CREATE TABLE IF NOT EXISTS clicks (
                id UUID PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_clicks_timestamp ON clicks (timestamp);",
        )
        .await
        .expect("Failed to create table");
}

#[post("/click")]
async fn register_click(pool: web::Data<Pool>) -> HttpResponse {
    let id = Uuid::new_v4();
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::BadRequest().finish(),
    };
    match client
        .execute(
            "INSERT INTO clicks (id, timestamp) VALUES ($1, NOW())",
            &[&id],
        )
        .await
    {
        Ok(_) => HttpResponse::Created().finish(),
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

#[get("/clicks")]
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

    let rows = match client.query(&sql, &[&datetime_utc]).await {
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

    let db_host = std::env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string());
    let db_port: u16 = std::env::var("DB_PORT")
        .unwrap_or_else(|_| "5432".to_string())
        .parse()
        .unwrap_or(5432);
    let db_user = std::env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string());
    let db_password = std::env::var("DB_PASSWORD").unwrap_or_else(|_| "postgres".to_string());
    let db_name = std::env::var("DB_NAME").unwrap_or_else(|_| "testdb".to_string());
    let port: u16 = std::env::var("PORT")
        .unwrap_or_else(|_| "5001".to_string())
        .parse()
        .unwrap_or(5001);

    let mut cfg = Config::new();
    cfg.host = Some(db_host);
    cfg.port = Some(db_port);
    cfg.user = Some(db_user);
    cfg.password = Some(db_password);
    cfg.dbname = Some(db_name);

    let pool = cfg
        .create_pool(Some(Runtime::Tokio1), NoTls)
        .expect("Failed to create pool");

    init_db(&pool).await;

    let pool_data = web::Data::new(pool);

    HttpServer::new(move || {
        App::new()
            .app_data(pool_data.clone())
            .service(register_click)
            .service(get_clicks)
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
