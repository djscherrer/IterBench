use actix_web::{web, App, HttpServer, HttpResponse};
use deadpool_postgres::{Config, Pool, Runtime};
use serde::{Deserialize, Serialize};
use tokio_postgres::NoTls;
use uuid::Uuid;
use chrono::{NaiveDate, Utc};

#[derive(Serialize)]
struct Click {
    id: String,
    timestamp: String,
}

async fn register_click(pool: web::Data<Pool>) -> HttpResponse {
    let id = Uuid::new_v4().to_string();
    let now = Utc::now();

    let client = match pool.get().await {
        Ok(client) => client,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let result = client
        .execute(
            "INSERT INTO clicks (id, timestamp) VALUES ($1, $2)",
            &[&id, &now],
        )
        .await;

    match result {
        Ok(_) => HttpResponse::Created().json(Click {
            id,
            timestamp: now.to_rfc3339(),
        }),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

#[derive(Deserialize)]
struct ClicksQuery {
    date: String,
    direction: String,
}

async fn get_clicks(pool: web::Data<Pool>, query: web::Query<ClicksQuery>) -> HttpResponse {
    let date = match NaiveDate::parse_from_str(&query.date, "%Y-%m-%d") {
        Ok(d) => d,
        Err(_) => {
            return HttpResponse::BadRequest().body("Invalid date format. Use YYYY-MM-DD.")
        }
    };

    let (sql_expr, date_val) = match query.direction.as_str() {
        "<" => ("timestamp::date < $1", date),
        ">" => ("timestamp::date > $1", date),
        "<=" => ("timestamp::date <= $1", date),
        ">=" => ("timestamp::date >= $1", date),
        _ => {
            return HttpResponse::BadRequest()
                .body("Invalid direction. Must be one of: <, >, <=, >=")
        }
    };

    let client = match pool.get().await {
        Ok(client) => client,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let sql = format!(
        "SELECT id, timestamp FROM clicks WHERE {} ORDER BY timestamp",
        sql_expr
    );

    let rows = match client.query(&sql, &[&date_val]).await {
        Ok(rows) => rows,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    if rows.is_empty() {
        return HttpResponse::NotFound().finish();
    }

    let clicks: Vec<Click> = rows
        .iter()
        .map(|row| {
            let id: String = row.get(0);
            let ts: chrono::DateTime<Utc> = row.get(1);
            Click {
                id,
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
        .expect("Failed to create database pool");

    // Initialize database schema
    {
        let client = pool
            .get()
            .await
            .expect("Failed to get DB client for initialization");
        client
            .batch_execute(
                "CREATE TABLE IF NOT EXISTS clicks (
                    id TEXT PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_clicks_timestamp ON clicks (timestamp);",
            )
            .await
            .expect("Failed to initialize database schema");
    }

    println!("Starting server on 0.0.0.0:{}", port);

    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(pool.clone()))
            .route("/click", web::post().to(register_click))
            .route("/clicks", web::get().to(get_clicks))
    })
    .bind(format!("0.0.0.0:{}", port))?
    .run()
    .await
}
