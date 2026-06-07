use actix_web::{web, App, HttpServer, HttpResponse};
use deadpool_postgres::{Config, Pool, Runtime};
use serde::{Deserialize, Serialize};
use tokio_postgres::NoTls;
use std::env;

// ── Models ──

#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
struct Pet {
    id: Option<i64>,
    name: String,
    photo_urls: Vec<String>,
    status: Option<String>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
struct Order {
    id: Option<i64>,
    pet_id: Option<i64>,
    quantity: Option<i32>,
    ship_date: Option<chrono::NaiveDateTime>,
    status: Option<String>,
    complete: Option<bool>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
struct User {
    id: Option<i64>,
    username: Option<String>,
    first_name: Option<String>,
    last_name: Option<String>,
    email: Option<String>,
    password: Option<String>,
    phone: Option<String>,
    user_status: Option<i32>,
}

// ── Query params ──

#[derive(Deserialize)]
struct FindByStatusQuery {
    status: String,
}

#[derive(Deserialize)]
struct LoginQuery {
    username: String,
    password: String,
}

// ── Database initialization ──

async fn init_db(pool: &Pool) -> Result<(), Box<dyn std::error::Error>> {
    let client = pool.get().await?;

    client
        .batch_execute(
            "
            CREATE TABLE IF NOT EXISTS pets (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                photo_urls TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'available'
            );

            CREATE TABLE IF NOT EXISTS orders (
                id BIGSERIAL PRIMARY KEY,
                pet_id BIGINT,
                quantity INTEGER,
                ship_date TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'placed',
                complete BOOLEAN NOT NULL DEFAULT FALSE
            );

            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                username TEXT UNIQUE,
                first_name TEXT,
                last_name TEXT,
                email TEXT,
                password TEXT,
                phone TEXT,
                user_status INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_pets_status ON pets(status);
            CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
            ",
        )
        .await?;

    println!("Database tables initialized");
    Ok(())
}

// ── Pet handlers ──

async fn add_pet(
    pool: web::Data<Pool>,
    body: web::Json<Pet>,
) -> HttpResponse {
    let pet = body.into_inner();
    let photo_urls_str = serde_json::to_string(&pet.photo_urls).unwrap_or_else(|_| "[]".to_string());
    let status = pet.status.unwrap_or_else(|| "available".to_string());

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query_one(
            "INSERT INTO pets (name, photo_urls, status) VALUES ($1, $2, $3) RETURNING id, name, photo_urls, status",
            &[&pet.name, &photo_urls_str, &status],
        )
        .await
    {
        Ok(row) => {
            let id: i64 = row.get(0);
            let name: String = row.get(1);
            let photo_urls_val: String = row.get(2);
            let status_val: String = row.get(3);
            let photo_urls: Vec<String> = serde_json::from_str(&photo_urls_val).unwrap_or_default();

            HttpResponse::Ok().json(Pet {
                id: Some(id),
                name,
                photo_urls,
                status: Some(status_val),
            })
        }
        Err(_) => HttpResponse::BadRequest().json("Invalid input"),
    }
}

async fn update_pet(
    pool: web::Data<Pool>,
    body: web::Json<Pet>,
) -> HttpResponse {
    let pet = body.into_inner();
    let pet_id = match pet.id {
        Some(id) => id,
        None => return HttpResponse::BadRequest().json("Invalid input"),
    };
    let photo_urls_str = serde_json::to_string(&pet.photo_urls).unwrap_or_else(|_| "[]".to_string());
    let status = pet.status.unwrap_or_else(|| "available".to_string());

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query_opt(
            "UPDATE pets SET name = $1, photo_urls = $2, status = $3 WHERE id = $4 RETURNING id, name, photo_urls, status",
            &[&pet.name, &photo_urls_str, &status, &pet_id],
        )
        .await
    {
        Ok(Some(row)) => {
            let id: i64 = row.get(0);
            let name: String = row.get(1);
            let photo_urls_val: String = row.get(2);
            let status_val: String = row.get(3);
            let photo_urls: Vec<String> = serde_json::from_str(&photo_urls_val).unwrap_or_default();

            HttpResponse::Ok().json(Pet {
                id: Some(id),
                name,
                photo_urls,
                status: Some(status_val),
            })
        }
        Ok(None) => HttpResponse::NotFound().json("Pet not found"),
        Err(_) => HttpResponse::BadRequest().json("Invalid input"),
    }
}

async fn find_pets_by_status(
    pool: web::Data<Pool>,
    query: web::Query<FindByStatusQuery>,
) -> HttpResponse {
    let status = &query.status;
    let valid_statuses = ["available", "pending", "sold"];
    if !valid_statuses.contains(&status.as_str()) {
        return HttpResponse::BadRequest().json("Invalid status value");
    }

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query(
            "SELECT id, name, photo_urls, status FROM pets WHERE status = $1",
            &[&status],
        )
        .await
    {
        Ok(rows) => {
            let pets: Vec<Pet> = rows
                .iter()
                .map(|row| {
                    let id: i64 = row.get(0);
                    let name: String = row.get(1);
                    let photo_urls_val: String = row.get(2);
                    let status_val: String = row.get(3);
                    let photo_urls: Vec<String> =
                        serde_json::from_str(&photo_urls_val).unwrap_or_default();
                    Pet {
                        id: Some(id),
                        name,
                        photo_urls,
                        status: Some(status_val),
                    }
                })
                .collect();
            HttpResponse::Ok().json(pets)
        }
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn get_pet_by_id(
    pool: web::Data<Pool>,
    path: web::Path<i64>,
) -> HttpResponse {
    let pet_id = path.into_inner();

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query_opt(
            "SELECT id, name, photo_urls, status FROM pets WHERE id = $1",
            &[&pet_id],
        )
        .await
    {
        Ok(Some(row)) => {
            let id: i64 = row.get(0);
            let name: String = row.get(1);
            let photo_urls_val: String = row.get(2);
            let status_val: String = row.get(3);
            let photo_urls: Vec<String> =
                serde_json::from_str(&photo_urls_val).unwrap_or_default();
            HttpResponse::Ok().json(Pet {
                id: Some(id),
                name,
                photo_urls,
                status: Some(status_val),
            })
        }
        Ok(None) => HttpResponse::NotFound().json("Pet not found"),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn delete_pet(
    pool: web::Data<Pool>,
    path: web::Path<i64>,
) -> HttpResponse {
    let pet_id = path.into_inner();

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .execute("DELETE FROM pets WHERE id = $1", &[&pet_id])
        .await
    {
        Ok(rows_affected) => {
            if rows_affected > 0 {
                HttpResponse::Ok().finish()
            } else {
                HttpResponse::NotFound().json("Pet not found")
            }
        }
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

// ── Order handlers ──

async fn place_order(
    pool: web::Data<Pool>,
    body: web::Json<Order>,
) -> HttpResponse {
    let order = body.into_inner();

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query_one(
            "INSERT INTO orders (pet_id, quantity, ship_date, status, complete) VALUES ($1, $2, $3, $4, $5) RETURNING id, pet_id, quantity, ship_date, status, complete",
            &[
                &order.pet_id,
                &order.quantity,
                &order.ship_date,
                &order.status.as_deref().unwrap_or("placed"),
                &order.complete.unwrap_or(false),
            ],
        )
        .await
    {
        Ok(row) => {
            let id: i64 = row.get(0);
            let pet_id: Option<i64> = row.get(1);
            let quantity: Option<i32> = row.get(2);
            let ship_date: Option<chrono::NaiveDateTime> = row.get(3);
            let status: String = row.get(4);
            let complete: bool = row.get(5);

            HttpResponse::Ok().json(Order {
                id: Some(id),
                pet_id,
                quantity,
                ship_date,
                status: Some(status),
                complete: Some(complete),
            })
        }
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn get_order_by_id(
    pool: web::Data<Pool>,
    path: web::Path<i64>,
) -> HttpResponse {
    let order_id = path.into_inner();

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query_opt(
            "SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id = $1",
            &[&order_id],
        )
        .await
    {
        Ok(Some(row)) => {
            let id: i64 = row.get(0);
            let pet_id: Option<i64> = row.get(1);
            let quantity: Option<i32> = row.get(2);
            let ship_date: Option<chrono::NaiveDateTime> = row.get(3);
            let status: String = row.get(4);
            let complete: bool = row.get(5);

            HttpResponse::Ok().json(Order {
                id: Some(id),
                pet_id,
                quantity,
                ship_date,
                status: Some(status),
                complete: Some(complete),
            })
        }
        Ok(None) => HttpResponse::NotFound().json("Order not found"),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn delete_order(
    pool: web::Data<Pool>,
    path: web::Path<i64>,
) -> HttpResponse {
    let order_id = path.into_inner();

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .execute("DELETE FROM orders WHERE id = $1", &[&order_id])
        .await
    {
        Ok(rows_affected) => {
            if rows_affected > 0 {
                HttpResponse::Ok().finish()
            } else {
                HttpResponse::NotFound().json("Order not found")
            }
        }
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

// ── User handlers ──

async fn create_user(
    pool: web::Data<Pool>,
    body: web::Json<User>,
) -> HttpResponse {
    let user = body.into_inner();

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query_one(
            "INSERT INTO users (username, first_name, last_name, email, password, phone, user_status) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id, username, first_name, last_name, email, password, phone, user_status",
            &[
                &user.username,
                &user.first_name,
                &user.last_name,
                &user.email,
                &user.password,
                &user.phone,
                &user.user_status.unwrap_or(0),
            ],
        )
        .await
    {
        Ok(row) => {
            let id: i64 = row.get(0);
            let username: Option<String> = row.get(1);
            let first_name: Option<String> = row.get(2);
            let last_name: Option<String> = row.get(3);
            let email: Option<String> = row.get(4);
            let password: Option<String> = row.get(5);
            let phone: Option<String> = row.get(6);
            let user_status: i32 = row.get(7);

            HttpResponse::Ok().json(User {
                id: Some(id),
                username,
                first_name,
                last_name,
                email,
                password,
                phone,
                user_status: Some(user_status),
            })
        }
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn get_user_by_name(
    pool: web::Data<Pool>,
    path: web::Path<String>,
) -> HttpResponse {
    let username = path.into_inner();

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query_opt(
            "SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username = $1",
            &[&username],
        )
        .await
    {
        Ok(Some(row)) => {
            let id: i64 = row.get(0);
            let username: Option<String> = row.get(1);
            let first_name: Option<String> = row.get(2);
            let last_name: Option<String> = row.get(3);
            let email: Option<String> = row.get(4);
            let password: Option<String> = row.get(5);
            let phone: Option<String> = row.get(6);
            let user_status: i32 = row.get(7);

            HttpResponse::Ok().json(User {
                id: Some(id),
                username,
                first_name,
                last_name,
                email,
                password,
                phone,
                user_status: Some(user_status),
            })
        }
        Ok(None) => HttpResponse::NotFound().json("User not found"),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn update_user(
    pool: web::Data<Pool>,
    path: web::Path<String>,
    body: web::Json<User>,
) -> HttpResponse {
    let path_username = path.into_inner();
    let user = body.into_inner();

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query_opt(
            "UPDATE users SET username = $1, first_name = $2, last_name = $3, email = $4, password = $5, phone = $6, user_status = $7 WHERE username = $8 RETURNING id, username, first_name, last_name, email, password, phone, user_status",
            &[
                &user.username,
                &user.first_name,
                &user.last_name,
                &user.email,
                &user.password,
                &user.phone,
                &user.user_status.unwrap_or(0),
                &path_username,
            ],
        )
        .await
    {
        Ok(Some(row)) => {
            let id: i64 = row.get(0);
            let username: Option<String> = row.get(1);
            let first_name: Option<String> = row.get(2);
            let last_name: Option<String> = row.get(3);
            let email: Option<String> = row.get(4);
            let password: Option<String> = row.get(5);
            let phone: Option<String> = row.get(6);
            let user_status: i32 = row.get(7);

            HttpResponse::Ok().json(User {
                id: Some(id),
                username,
                first_name,
                last_name,
                email,
                password,
                phone,
                user_status: Some(user_status),
            })
        }
        Ok(None) => HttpResponse::NotFound().json("User not found"),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn delete_user(
    pool: web::Data<Pool>,
    path: web::Path<String>,
) -> HttpResponse {
    let username = path.into_inner();

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .execute("DELETE FROM users WHERE username = $1", &[&username])
        .await
    {
        Ok(rows_affected) => {
            if rows_affected > 0 {
                HttpResponse::Ok().finish()
            } else {
                HttpResponse::NotFound().json("User not found")
            }
        }
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn login_user(
    pool: web::Data<Pool>,
    query: web::Query<LoginQuery>,
) -> HttpResponse {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query_opt(
            "SELECT 1 FROM users WHERE username = $1 AND password = $2",
            &[&query.username, &query.password],
        )
        .await
    {
        Ok(Some(_)) => HttpResponse::Ok().json("ok"),
        Ok(None) => HttpResponse::BadRequest().json("Invalid credentials"),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

// ── Main ──

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
    let server_port: u16 = env::var("PORT")
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

    init_db(&pool)
        .await
        .expect("Failed to initialize database");

    println!("Starting server on 0.0.0.0:{}", server_port);

    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(pool.clone()))
            // Pet routes
            .route("/pet", web::post().to(add_pet))
            .route("/pet", web::put().to(update_pet))
            .route("/pet/findByStatus", web::get().to(find_pets_by_status))
            .route("/pet/{petId}", web::get().to(get_pet_by_id))
            .route("/pet/{petId}", web::delete().to(delete_pet))
            // Order routes
            .route("/store/order", web::post().to(place_order))
            .route("/store/order/{orderId}", web::get().to(get_order_by_id))
            .route("/store/order/{orderId}", web::delete().to(delete_order))
            // User routes
            .route("/user/login", web::get().to(login_user))
            .route("/user", web::post().to(create_user))
            .route("/user/{username}", web::get().to(get_user_by_name))
            .route("/user/{username}", web::put().to(update_user))
            .route("/user/{username}", web::delete().to(delete_user))
    })
    .bind(("0.0.0.0", server_port))?
    .workers(
        std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(4)
    )
    .run()
    .await
}
