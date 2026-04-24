use actix_web::{web, App, HttpServer, HttpResponse};
use deadpool_postgres::{Config, Pool, Runtime, ManagerConfig, RecyclingMethod};
use serde::{Deserialize, Serialize};
use tokio_postgres::NoTls;
use std::env;

// ── Models ──────────────────────────────────────────────────────────────────

#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
struct Pet {
    #[serde(default)]
    id: Option<i64>,
    name: String,
    photo_urls: Vec<String>,
    #[serde(default)]
    status: Option<String>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
struct Order {
    #[serde(default)]
    id: Option<i64>,
    #[serde(default)]
    pet_id: Option<i64>,
    #[serde(default)]
    quantity: Option<i32>,
    #[serde(default)]
    ship_date: Option<chrono::NaiveDateTime>,
    #[serde(default)]
    status: Option<String>,
    #[serde(default)]
    complete: Option<bool>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
struct User {
    #[serde(default)]
    id: Option<i64>,
    #[serde(default)]
    username: Option<String>,
    #[serde(default)]
    first_name: Option<String>,
    #[serde(default)]
    last_name: Option<String>,
    #[serde(default)]
    email: Option<String>,
    #[serde(default)]
    password: Option<String>,
    #[serde(default)]
    phone: Option<String>,
    #[serde(default)]
    user_status: Option<i32>,
}

#[derive(Deserialize)]
struct FindByStatusQuery {
    status: String,
}

#[derive(Deserialize)]
struct LoginQuery {
    username: String,
    password: String,
}

// ── Database initialization ─────────────────────────────────────────────────

async fn init_db(pool: &Pool) {
    let client = pool.get().await.expect("Failed to get DB connection for init");
    client.batch_execute("
        CREATE TABLE IF NOT EXISTS pets (
            id BIGSERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            photo_urls TEXT[] NOT NULL DEFAULT '{}',
            status TEXT DEFAULT 'available'
        );
        CREATE INDEX IF NOT EXISTS idx_pets_status ON pets(status);

        CREATE TABLE IF NOT EXISTS orders (
            id BIGSERIAL PRIMARY KEY,
            pet_id BIGINT,
            quantity INT DEFAULT 0,
            ship_date TIMESTAMP,
            status TEXT DEFAULT 'placed',
            complete BOOLEAN DEFAULT false
        );

        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            username TEXT UNIQUE,
            first_name TEXT,
            last_name TEXT,
            email TEXT,
            password TEXT,
            phone TEXT,
            user_status INT DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
    ").await.expect("Failed to initialize database tables");
}

// ── Pet handlers ────────────────────────────────────────────────────────────

async fn add_pet(pool: web::Data<Pool>, body: web::Json<Pet>) -> HttpResponse {
    let pet = body.into_inner();
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let row = client.query_one(
        "INSERT INTO pets (name, photo_urls, status) VALUES ($1, $2, $3) RETURNING id, name, photo_urls, status",
        &[&pet.name, &pet.photo_urls, &pet.status.as_deref().unwrap_or("available")],
    ).await;
    match row {
        Ok(row) => {
            let p = Pet {
                id: Some(row.get(0)),
                name: row.get(1),
                photo_urls: row.get(2),
                status: row.get(3),
            };
            HttpResponse::Ok().json(p)
        }
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

async fn update_pet(pool: web::Data<Pool>, body: web::Json<Pet>) -> HttpResponse {
    let pet = body.into_inner();
    let id = match pet.id {
        Some(id) => id,
        None => return HttpResponse::NotFound().finish(),
    };
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let result = client.query_opt(
        "UPDATE pets SET name=$1, photo_urls=$2, status=$3 WHERE id=$4 RETURNING id, name, photo_urls, status",
        &[&pet.name, &pet.photo_urls, &pet.status.as_deref().unwrap_or("available"), &id],
    ).await;
    match result {
        Ok(Some(row)) => {
            let p = Pet {
                id: Some(row.get(0)),
                name: row.get(1),
                photo_urls: row.get(2),
                status: row.get(3),
            };
            HttpResponse::Ok().json(p)
        }
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn find_pets_by_status(pool: web::Data<Pool>, query: web::Query<FindByStatusQuery>) -> HttpResponse {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let rows = client.query(
        "SELECT id, name, photo_urls, status FROM pets WHERE status = $1",
        &[&query.status],
    ).await;
    match rows {
        Ok(rows) => {
            let pets: Vec<Pet> = rows.iter().map(|row| Pet {
                id: Some(row.get(0)),
                name: row.get(1),
                photo_urls: row.get(2),
                status: row.get(3),
            }).collect();
            HttpResponse::Ok().json(pets)
        }
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn get_pet_by_id(pool: web::Data<Pool>, path: web::Path<i64>) -> HttpResponse {
    let pet_id = path.into_inner();
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let result = client.query_opt(
        "SELECT id, name, photo_urls, status FROM pets WHERE id = $1",
        &[&pet_id],
    ).await;
    match result {
        Ok(Some(row)) => {
            let p = Pet {
                id: Some(row.get(0)),
                name: row.get(1),
                photo_urls: row.get(2),
                status: row.get(3),
            };
            HttpResponse::Ok().json(p)
        }
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn delete_pet(pool: web::Data<Pool>, path: web::Path<i64>) -> HttpResponse {
    let pet_id = path.into_inner();
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let result = client.execute("DELETE FROM pets WHERE id = $1", &[&pet_id]).await;
    match result {
        Ok(count) if count > 0 => HttpResponse::Ok().finish(),
        Ok(_) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

// ── Order handlers ──────────────────────────────────────────────────────────

async fn place_order(pool: web::Data<Pool>, body: web::Json<Order>) -> HttpResponse {
    let order = body.into_inner();
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let row = client.query_one(
        "INSERT INTO orders (pet_id, quantity, ship_date, status, complete) VALUES ($1, $2, $3, $4, $5) RETURNING id, pet_id, quantity, ship_date, status, complete",
        &[
            &order.pet_id,
            &order.quantity.unwrap_or(0),
            &order.ship_date,
            &order.status.as_deref().unwrap_or("placed"),
            &order.complete.unwrap_or(false),
        ],
    ).await;
    match row {
        Ok(row) => {
            let o = Order {
                id: Some(row.get(0)),
                pet_id: row.get(1),
                quantity: row.get(2),
                ship_date: row.get(3),
                status: row.get(4),
                complete: row.get(5),
            };
            HttpResponse::Ok().json(o)
        }
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn get_order_by_id(pool: web::Data<Pool>, path: web::Path<i64>) -> HttpResponse {
    let order_id = path.into_inner();
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let result = client.query_opt(
        "SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id = $1",
        &[&order_id],
    ).await;
    match result {
        Ok(Some(row)) => {
            let o = Order {
                id: Some(row.get(0)),
                pet_id: row.get(1),
                quantity: row.get(2),
                ship_date: row.get(3),
                status: row.get(4),
                complete: row.get(5),
            };
            HttpResponse::Ok().json(o)
        }
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn delete_order(pool: web::Data<Pool>, path: web::Path<i64>) -> HttpResponse {
    let order_id = path.into_inner();
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let result = client.execute("DELETE FROM orders WHERE id = $1", &[&order_id]).await;
    match result {
        Ok(count) if count > 0 => HttpResponse::Ok().finish(),
        Ok(_) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

// ── User handlers ───────────────────────────────────────────────────────────

async fn create_user(pool: web::Data<Pool>, body: web::Json<User>) -> HttpResponse {
    let user = body.into_inner();
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let row = client.query_one(
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
    ).await;
    match row {
        Ok(row) => {
            let u = User {
                id: Some(row.get(0)),
                username: row.get(1),
                first_name: row.get(2),
                last_name: row.get(3),
                email: row.get(4),
                password: row.get(5),
                phone: row.get(6),
                user_status: row.get(7),
            };
            HttpResponse::Ok().json(u)
        }
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn get_user_by_name(pool: web::Data<Pool>, path: web::Path<String>) -> HttpResponse {
    let username = path.into_inner();
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let result = client.query_opt(
        "SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username = $1",
        &[&username],
    ).await;
    match result {
        Ok(Some(row)) => {
            let u = User {
                id: Some(row.get(0)),
                username: row.get(1),
                first_name: row.get(2),
                last_name: row.get(3),
                email: row.get(4),
                password: row.get(5),
                phone: row.get(6),
                user_status: row.get(7),
            };
            HttpResponse::Ok().json(u)
        }
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn update_user(pool: web::Data<Pool>, path: web::Path<String>, body: web::Json<User>) -> HttpResponse {
    let username = path.into_inner();
    let user = body.into_inner();
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let result = client.query_opt(
        "UPDATE users SET username=$1, first_name=$2, last_name=$3, email=$4, password=$5, phone=$6, user_status=$7 WHERE username=$8 RETURNING id, username, first_name, last_name, email, password, phone, user_status",
        &[
            &user.username,
            &user.first_name,
            &user.last_name,
            &user.email,
            &user.password,
            &user.phone,
            &user.user_status.unwrap_or(0),
            &username,
        ],
    ).await;
    match result {
        Ok(Some(row)) => {
            let u = User {
                id: Some(row.get(0)),
                username: row.get(1),
                first_name: row.get(2),
                last_name: row.get(3),
                email: row.get(4),
                password: row.get(5),
                phone: row.get(6),
                user_status: row.get(7),
            };
            HttpResponse::Ok().json(u)
        }
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn delete_user(pool: web::Data<Pool>, path: web::Path<String>) -> HttpResponse {
    let username = path.into_inner();
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let result = client.execute("DELETE FROM users WHERE username = $1", &[&username]).await;
    match result {
        Ok(count) if count > 0 => HttpResponse::Ok().finish(),
        Ok(_) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn login_user(pool: web::Data<Pool>, query: web::Query<LoginQuery>) -> HttpResponse {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };
    let result = client.query_opt(
        "SELECT id FROM users WHERE username = $1 AND password = $2",
        &[&query.username, &query.password],
    ).await;
    match result {
        Ok(Some(_)) => HttpResponse::Ok().json("Login successful"),
        Ok(None) => HttpResponse::BadRequest().body("Invalid credentials"),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

// ── Main ────────────────────────────────────────────────────────────────────

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let db_host = env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string());
    let db_port: u16 = env::var("DB_PORT").unwrap_or_else(|_| "5432".to_string()).parse().unwrap_or(5432);
    let db_user = env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string());
    let db_password = env::var("DB_PASSWORD").unwrap_or_else(|_| "postgres".to_string());
    let db_name = env::var("DB_NAME").unwrap_or_else(|_| "testdb".to_string());
    let port: u16 = env::var("PORT").unwrap_or_else(|_| "5001".to_string()).parse().unwrap_or(5001);

    let mut cfg = Config::new();
    cfg.host = Some(db_host);
    cfg.port = Some(db_port);
    cfg.user = Some(db_user);
    cfg.password = Some(db_password);
    cfg.dbname = Some(db_name);
    cfg.manager = Some(ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    });
    let pool = cfg.create_pool(Some(Runtime::Tokio1), NoTls)
        .expect("Failed to create pool");

    init_db(&pool).await;

    let pool_data = web::Data::new(pool);

    HttpServer::new(move || {
        App::new()
            .app_data(pool_data.clone())
            .app_data(web::JsonConfig::default().limit(1048576))
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
