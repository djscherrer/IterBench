use actix_web::{web, App, HttpResponse, HttpServer, Responder};
use deadpool_postgres::{Config, ManagerConfig, Pool, RecyclingMethod, Runtime};
use serde::{Deserialize, Serialize};
use tokio_postgres::NoTls;
use chrono::{DateTime, Utc};
use std::env;

#[derive(Debug, Serialize, Deserialize, Clone)]
struct Pet {
    #[serde(skip_serializing_if = "Option::is_none")]
    id: Option<i64>,
    name: String,
    #[serde(rename = "photoUrls")]
    photo_urls: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    status: Option<String>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct Order {
    #[serde(skip_serializing_if = "Option::is_none")]
    id: Option<i64>,
    #[serde(rename = "petId", skip_serializing_if = "Option::is_none")]
    pet_id: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    quantity: Option<i32>,
    #[serde(rename = "shipDate", skip_serializing_if = "Option::is_none")]
    ship_date: Option<DateTime<Utc>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    status: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    complete: Option<bool>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct User {
    #[serde(skip_serializing_if = "Option::is_none")]
    id: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    username: Option<String>,
    #[serde(rename = "firstName", skip_serializing_if = "Option::is_none")]
    first_name: Option<String>,
    #[serde(rename = "lastName", skip_serializing_if = "Option::is_none")]
    last_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    email: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    password: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    phone: Option<String>,
    #[serde(rename = "userStatus", skip_serializing_if = "Option::is_none")]
    user_status: Option<i32>,
}

#[derive(Debug, Deserialize)]
struct StatusQuery {
    status: String,
}

#[derive(Debug, Deserialize)]
struct LoginQuery {
    username: String,
    password: String,
}

async fn init_db(pool: &Pool) -> Result<(), Box<dyn std::error::Error>> {
    let client = pool.get().await?;
    client.batch_execute(
        "
        CREATE TABLE IF NOT EXISTS pets (
            id BIGSERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            photo_urls TEXT[] NOT NULL DEFAULT '{}',
            status TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pets_status ON pets(status);

        CREATE TABLE IF NOT EXISTS orders (
            id BIGSERIAL PRIMARY KEY,
            pet_id BIGINT,
            quantity INTEGER,
            ship_date TIMESTAMPTZ,
            status TEXT,
            complete BOOLEAN
        );

        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            username TEXT UNIQUE,
            first_name TEXT,
            last_name TEXT,
            email TEXT,
            password TEXT,
            phone TEXT,
            user_status INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
        ",
    )
    .await?;
    Ok(())
}

fn row_to_pet(row: &tokio_postgres::Row) -> Pet {
    Pet {
        id: row.get("id"),
        name: row.get("name"),
        photo_urls: row.get::<_, Vec<String>>("photo_urls"),
        status: row.get("status"),
    }
}

fn row_to_order(row: &tokio_postgres::Row) -> Order {
    Order {
        id: row.get("id"),
        pet_id: row.get("pet_id"),
        quantity: row.get("quantity"),
        ship_date: row.get("ship_date"),
        status: row.get("status"),
        complete: row.get("complete"),
    }
}

fn row_to_user(row: &tokio_postgres::Row) -> User {
    User {
        id: row.get("id"),
        username: row.get("username"),
        first_name: row.get("first_name"),
        last_name: row.get("last_name"),
        email: row.get("email"),
        password: row.get("password"),
        phone: row.get("phone"),
        user_status: row.get("user_status"),
    }
}

// ============ Pet handlers ============

async fn add_pet(pool: web::Data<Pool>, pet: web::Json<Pet>) -> impl Responder {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let pet = pet.into_inner();
    let result = if let Some(id) = pet.id {
        client.query_one(
            "INSERT INTO pets (id, name, photo_urls, status) VALUES ($1, $2, $3, $4) \
             ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, photo_urls = EXCLUDED.photo_urls, status = EXCLUDED.status \
             RETURNING id, name, photo_urls, status",
            &[&id, &pet.name, &pet.photo_urls, &pet.status],
        ).await
    } else {
        client.query_one(
            "INSERT INTO pets (name, photo_urls, status) VALUES ($1, $2, $3) RETURNING id, name, photo_urls, status",
            &[&pet.name, &pet.photo_urls, &pet.status],
        ).await
    };
    match result {
        Ok(row) => HttpResponse::Ok().json(row_to_pet(&row)),
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

async fn update_pet(pool: web::Data<Pool>, pet: web::Json<Pet>) -> impl Responder {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let pet = pet.into_inner();
    let id = match pet.id {
        Some(i) => i,
        None => return HttpResponse::NotFound().finish(),
    };
    match client.query_opt(
        "UPDATE pets SET name = $1, photo_urls = $2, status = $3 WHERE id = $4 RETURNING id, name, photo_urls, status",
        &[&pet.name, &pet.photo_urls, &pet.status, &id],
    ).await {
        Ok(Some(row)) => HttpResponse::Ok().json(row_to_pet(&row)),
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::NotFound().finish(),
    }
}

async fn find_pets_by_status(pool: web::Data<Pool>, q: web::Query<StatusQuery>) -> impl Responder {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    match client.query(
        "SELECT id, name, photo_urls, status FROM pets WHERE status = $1",
        &[&q.status],
    ).await {
        Ok(rows) => {
            let pets: Vec<Pet> = rows.iter().map(row_to_pet).collect();
            HttpResponse::Ok().json(pets)
        }
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn get_pet_by_id(pool: web::Data<Pool>, path: web::Path<i64>) -> impl Responder {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let id = path.into_inner();
    match client.query_opt(
        "SELECT id, name, photo_urls, status FROM pets WHERE id = $1",
        &[&id],
    ).await {
        Ok(Some(row)) => HttpResponse::Ok().json(row_to_pet(&row)),
        _ => HttpResponse::NotFound().finish(),
    }
}

async fn delete_pet(pool: web::Data<Pool>, path: web::Path<i64>) -> impl Responder {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let id = path.into_inner();
    match client.execute("DELETE FROM pets WHERE id = $1", &[&id]).await {
        Ok(n) if n > 0 => HttpResponse::Ok().finish(),
        _ => HttpResponse::NotFound().finish(),
    }
}

// ============ Order handlers ============

async fn place_order(pool: web::Data<Pool>, order: web::Json<Order>) -> impl Responder {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let order = order.into_inner();
    let result = if let Some(id) = order.id {
        client.query_one(
            "INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete) VALUES ($1, $2, $3, $4, $5, $6) \
             ON CONFLICT (id) DO UPDATE SET pet_id = EXCLUDED.pet_id, quantity = EXCLUDED.quantity, ship_date = EXCLUDED.ship_date, status = EXCLUDED.status, complete = EXCLUDED.complete \
             RETURNING id, pet_id, quantity, ship_date, status, complete",
            &[&id, &order.pet_id, &order.quantity, &order.ship_date, &order.status, &order.complete],
        ).await
    } else {
        client.query_one(
            "INSERT INTO orders (pet_id, quantity, ship_date, status, complete) VALUES ($1, $2, $3, $4, $5) RETURNING id, pet_id, quantity, ship_date, status, complete",
            &[&order.pet_id, &order.quantity, &order.ship_date, &order.status, &order.complete],
        ).await
    };
    match result {
        Ok(row) => HttpResponse::Ok().json(row_to_order(&row)),
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

async fn get_order_by_id(pool: web::Data<Pool>, path: web::Path<i64>) -> impl Responder {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let id = path.into_inner();
    match client.query_opt(
        "SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id = $1",
        &[&id],
    ).await {
        Ok(Some(row)) => HttpResponse::Ok().json(row_to_order(&row)),
        _ => HttpResponse::NotFound().finish(),
    }
}

async fn delete_order(pool: web::Data<Pool>, path: web::Path<i64>) -> impl Responder {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let id = path.into_inner();
    match client.execute("DELETE FROM orders WHERE id = $1", &[&id]).await {
        Ok(n) if n > 0 => HttpResponse::Ok().finish(),
        _ => HttpResponse::NotFound().finish(),
    }
}

// ============ User handlers ============

async fn create_user(pool: web::Data<Pool>, user: web::Json<User>) -> impl Responder {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let user = user.into_inner();
    let result = if let Some(id) = user.id {
        client.query_one(
            "INSERT INTO users (id, username, first_name, last_name, email, password, phone, user_status) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8) \
             ON CONFLICT (id) DO UPDATE SET username = EXCLUDED.username, first_name = EXCLUDED.first_name, last_name = EXCLUDED.last_name, email = EXCLUDED.email, password = EXCLUDED.password, phone = EXCLUDED.phone, user_status = EXCLUDED.user_status \
             RETURNING id, username, first_name, last_name, email, password, phone, user_status",
            &[&id, &user.username, &user.first_name, &user.last_name, &user.email, &user.password, &user.phone, &user.user_status],
        ).await
    } else {
        client.query_one(
            "INSERT INTO users (username, first_name, last_name, email, password, phone, user_status) \
             VALUES ($1, $2, $3, $4, $5, $6, $7) \
             RETURNING id, username, first_name, last_name, email, password, phone, user_status",
            &[&user.username, &user.first_name, &user.last_name, &user.email, &user.password, &user.phone, &user.user_status],
        ).await
    };
    match result {
        Ok(row) => HttpResponse::Ok().json(row_to_user(&row)),
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

async fn get_user_by_name(pool: web::Data<Pool>, path: web::Path<String>) -> impl Responder {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let username = path.into_inner();
    match client.query_opt(
        "SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username = $1",
        &[&username],
    ).await {
        Ok(Some(row)) => HttpResponse::Ok().json(row_to_user(&row)),
        _ => HttpResponse::NotFound().finish(),
    }
}

async fn update_user(pool: web::Data<Pool>, path: web::Path<String>, user: web::Json<User>) -> impl Responder {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let username = path.into_inner();
    let user = user.into_inner();
    match client.query_opt(
        "UPDATE users SET username = COALESCE($1, username), first_name = $2, last_name = $3, email = $4, password = $5, phone = $6, user_status = $7 \
         WHERE username = $8 RETURNING id, username, first_name, last_name, email, password, phone, user_status",
        &[&user.username, &user.first_name, &user.last_name, &user.email, &user.password, &user.phone, &user.user_status, &username],
    ).await {
        Ok(Some(row)) => HttpResponse::Ok().json(row_to_user(&row)),
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::NotFound().finish(),
    }
}

async fn delete_user(pool: web::Data<Pool>, path: web::Path<String>) -> impl Responder {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let username = path.into_inner();
    match client.execute("DELETE FROM users WHERE username = $1", &[&username]).await {
        Ok(n) if n > 0 => HttpResponse::Ok().finish(),
        _ => HttpResponse::NotFound().finish(),
    }
}

async fn login_user(pool: web::Data<Pool>, q: web::Query<LoginQuery>) -> impl Responder {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    match client.query_opt(
        "SELECT id FROM users WHERE username = $1 AND password = $2",
        &[&q.username, &q.password],
    ).await {
        Ok(Some(_)) => HttpResponse::Ok().json(format!("logged in user session: {}", q.username)),
        Ok(None) => HttpResponse::BadRequest().finish(),
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init_from_env(env_logger::Env::new().default_filter_or("info"));

    let db_host = env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string());
    let db_port: u16 = env::var("DB_PORT").ok().and_then(|p| p.parse().ok()).unwrap_or(5432);
    let db_user = env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string());
    let db_password = env::var("DB_PASSWORD").unwrap_or_else(|_| "postgres".to_string());
    let db_name = env::var("DB_NAME").unwrap_or_else(|_| "testdb".to_string());

    let mut cfg = Config::new();
    cfg.host = Some(db_host);
    cfg.port = Some(db_port);
    cfg.user = Some(db_user);
    cfg.password = Some(db_password);
    cfg.dbname = Some(db_name);
    cfg.manager = Some(ManagerConfig { recycling_method: RecyclingMethod::Fast });

    let pool = cfg.create_pool(Some(Runtime::Tokio1), NoTls).expect("Failed to create pool");

    // Retry init
    for attempt in 0..30 {
        match init_db(&pool).await {
            Ok(_) => {
                eprintln!("Database initialized");
                break;
            }
            Err(e) => {
                eprintln!("DB init attempt {} failed: {}", attempt, e);
                if attempt == 29 {
                    eprintln!("Giving up on DB init");
                }
                tokio::time::sleep(std::time::Duration::from_secs(1)).await;
            }
        }
    }

    let port: u16 = env::var("PORT").ok().and_then(|p| p.parse().ok()).unwrap_or(5001);
    eprintln!("Starting server on 0.0.0.0:{}", port);

    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(pool.clone()))
            .app_data(web::JsonConfig::default().limit(4 * 1024 * 1024))
            .route("/pet", web::post().to(add_pet))
            .route("/pet", web::put().to(update_pet))
            .route("/pet/findByStatus", web::get().to(find_pets_by_status))
            .route("/pet/{petId}", web::get().to(get_pet_by_id))
            .route("/pet/{petId}", web::delete().to(delete_pet))
            .route("/store/order", web::post().to(place_order))
            .route("/store/order/{orderId}", web::get().to(get_order_by_id))
            .route("/store/order/{orderId}", web::delete().to(delete_order))
            .route("/user", web::post().to(create_user))
            .route("/user/login", web::get().to(login_user))
            .route("/user/{username}", web::get().to(get_user_by_name))
            .route("/user/{username}", web::put().to(update_user))
            .route("/user/{username}", web::delete().to(delete_user))
    })
    .bind(("0.0.0.0", port))?
    .run()
    .await
}
