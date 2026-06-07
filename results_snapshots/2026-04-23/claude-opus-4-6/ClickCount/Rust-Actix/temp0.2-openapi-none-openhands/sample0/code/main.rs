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
        .execute(
            "CREATE TABLE IF NOT EXISTS clicks (
                id UUID PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )",
            &[],
        )
        .await
        .expect("Failed to create clicks table");
}

#[post("/click")]
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

#[get("/clicks")]
async fn get_clicks(pool: web::Data<Pool>, query: web::Query<ClicksQuery>) -> HttpResponse {
    let direction = match query.direction.as_str() {
        "<" | ">" | "<=" | ">=" => query.direction.as_str(),
        _ => return HttpResponse::BadRequest().body("Invalid direction parameter"),
    };

    let date = match NaiveDate::parse_from_str(&query.date, "%Y-%m-%d") {
        Ok(d) => d,
        Err(_) => return HttpResponse::BadRequest().body("Invalid date format"),
    };

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::BadRequest().finish(),
    };

    let sql = format!(
        "SELECT id, timestamp FROM clicks WHERE timestamp::date {} $1 ORDER BY timestamp",
        direction
    );

    let rows = match client.query(&sql, &[&date]).await {
        Ok(r) => r,
        Err(_) => return HttpResponse::BadRequest().finish(),
    };

    if rows.is_empty() {
        return HttpResponse::NotFound().body("No clicks found");
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
        .unwrap_or_else(|_| "5001".to_string())
        .parse()
        .expect("PORT must be a number");

    let mut cfg = Config::new();
    cfg.host = Some(std::env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string()));
    cfg.port = Some(
        std::env::var("DB_PORT")
            .unwrap_or_else(|_| "5432".to_string())
            .parse()
            .expect("DB_PORT must be a number"),
    );
    cfg.user = Some(std::env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string()));
    cfg.password = Some(std::env::var("DB_PASSWORD").unwrap_or_else(|_| "postgres".to_string()));
    cfg.dbname = Some(std::env::var("DB_NAME").unwrap_or_else(|_| "testdb".to_string()));

    let pool = cfg
        .create_pool(Some(Runtime::Tokio1), NoTls)
        .expect("Failed to create pool");

    init_db(&pool).await;

    println!("Server running on 0.0.0.0:{}", port);

    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(pool.clone()))
            .service(register_click)
            .service(get_clicks)
    })
    .bind(("0.0.0.0", port))?
    .run()
    .await
}
