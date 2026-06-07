use actix_web::{web, App, HttpServer, HttpResponse};
use deadpool_postgres::{Config, Pool, Runtime, ManagerConfig, RecyclingMethod};
use serde::{Deserialize, Serialize};
use tokio_postgres::NoTls;
use std::env;
use std::sync::atomic::{AtomicI64, Ordering};

// --- Models ---

#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
struct Pet {
    #[serde(default)]
    id: Option<i64>,
    name: String,
    photo_urls: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    status: Option<String>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
struct Order {
    #[serde(default)]
    id: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pet_id: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    quantity: Option<i32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    ship_date: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    status: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    complete: Option<bool>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
struct User {
    #[serde(default)]
    id: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    username: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    first_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    last_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    email: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    password: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    phone: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
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

struct AppState {
    pool: Pool,
    pet_id_counter: AtomicI64,
    order_id_counter: AtomicI64,
    user_id_counter: AtomicI64,
}

// --- DB Init ---

async fn init_db(pool: &Pool) -> Result<(), Box<dyn std::error::Error>> {
    let client = pool.get().await?;
    client.batch_execute("
        CREATE TABLE IF NOT EXISTS pets (
            id BIGINT PRIMARY KEY,
            name TEXT NOT NULL,
            photo_urls TEXT[] NOT NULL,
            status TEXT DEFAULT 'available'
        );
        CREATE INDEX IF NOT EXISTS idx_pets_status ON pets(status);

        CREATE TABLE IF NOT EXISTS orders (
            id BIGINT PRIMARY KEY,
            pet_id BIGINT,
            quantity INT,
            ship_date TEXT,
            status TEXT DEFAULT 'placed',
            complete BOOLEAN DEFAULT false
        );

        CREATE TABLE IF NOT EXISTS users (
            id BIGINT PRIMARY KEY,
            username TEXT UNIQUE,
            first_name TEXT,
            last_name TEXT,
            email TEXT,
            password TEXT,
            phone TEXT,
            user_status INT DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
    ").await?;
    Ok(())
}

async fn init_id_counters(pool: &Pool) -> Result<(i64, i64, i64), Box<dyn std::error::Error>> {
    let client = pool.get().await?;
    let pet_max: i64 = client.query_one("SELECT COALESCE(MAX(id), 0) FROM pets", &[]).await?.get(0);
    let order_max: i64 = client.query_one("SELECT COALESCE(MAX(id), 0) FROM orders", &[]).await?.get(0);
    let user_max: i64 = client.query_one("SELECT COALESCE(MAX(id), 0) FROM users", &[]).await?.get(0);
    Ok((pet_max, order_max, user_max))
}

// --- Pet Handlers ---

async fn add_pet(data: web::Data<AppState>, body: web::Json<Pet>) -> HttpResponse {
    let mut pet = body.into_inner();
    let id = pet.id.unwrap_or_else(|| data.pet_id_counter.fetch_add(1, Ordering::Relaxed) + 1);
    // Update counter if provided id is higher
    loop {
        let current = data.pet_id_counter.load(Ordering::Relaxed);
        if id <= current { break; }
        if data.pet_id_counter.compare_exchange(current, id, Ordering::Relaxed, Ordering::Relaxed).is_ok() { break; }
    }
    pet.id = Some(id);
    let status = pet.status.clone().unwrap_or_else(|| "available".to_string());

    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let result = client.execute(
        "INSERT INTO pets (id, name, photo_urls, status) VALUES ($1, $2, $3, $4) ON CONFLICT (id) DO UPDATE SET name = $2, photo_urls = $3, status = $4",
        &[&id, &pet.name, &pet.photo_urls, &status],
    ).await;

    match result {
        Ok(_) => {
            pet.status = Some(status);
            HttpResponse::Ok().json(&pet)
        }
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

async fn update_pet(data: web::Data<AppState>, body: web::Json<Pet>) -> HttpResponse {
    let pet = body.into_inner();
    let id = match pet.id {
        Some(id) => id,
        None => return HttpResponse::NotFound().finish(),
    };
    let status = pet.status.clone().unwrap_or_else(|| "available".to_string());

    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let result = client.execute(
        "UPDATE pets SET name = $2, photo_urls = $3, status = $4 WHERE id = $1",
        &[&id, &pet.name, &pet.photo_urls, &status],
    ).await;

    match result {
        Ok(0) => HttpResponse::NotFound().finish(),
        Ok(_) => {
            let mut p = pet;
            p.status = Some(status);
            HttpResponse::Ok().json(&p)
        }
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn find_pets_by_status(data: web::Data<AppState>, query: web::Query<FindByStatusQuery>) -> HttpResponse {
    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let rows = client.query(
        "SELECT id, name, photo_urls, status FROM pets WHERE status = $1",
        &[&query.status],
    ).await;

    match rows {
        Ok(rows) => {
            let pets: Vec<Pet> = rows.iter().map(|row| {
                Pet {
                    id: Some(row.get(0)),
                    name: row.get(1),
                    photo_urls: row.get(2),
                    status: row.get(3),
                }
            }).collect();
            HttpResponse::Ok().json(&pets)
        }
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn get_pet_by_id(data: web::Data<AppState>, path: web::Path<i64>) -> HttpResponse {
    let pet_id = path.into_inner();
    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let result = client.query_opt(
        "SELECT id, name, photo_urls, status FROM pets WHERE id = $1",
        &[&pet_id],
    ).await;

    match result {
        Ok(Some(row)) => {
            let pet = Pet {
                id: Some(row.get(0)),
                name: row.get(1),
                photo_urls: row.get(2),
                status: row.get(3),
            };
            HttpResponse::Ok().json(&pet)
        }
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn delete_pet(data: web::Data<AppState>, path: web::Path<i64>) -> HttpResponse {
    let pet_id = path.into_inner();
    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let result = client.execute("DELETE FROM pets WHERE id = $1", &[&pet_id]).await;

    match result {
        Ok(0) => HttpResponse::NotFound().finish(),
        Ok(_) => HttpResponse::Ok().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

// --- Order Handlers ---

async fn place_order(data: web::Data<AppState>, body: web::Json<Order>) -> HttpResponse {
    let mut order = body.into_inner();
    let id = order.id.unwrap_or_else(|| data.order_id_counter.fetch_add(1, Ordering::Relaxed) + 1);
    loop {
        let current = data.order_id_counter.load(Ordering::Relaxed);
        if id <= current { break; }
        if data.order_id_counter.compare_exchange(current, id, Ordering::Relaxed, Ordering::Relaxed).is_ok() { break; }
    }
    order.id = Some(id);
    let status = order.status.clone().unwrap_or_else(|| "placed".to_string());
    let complete = order.complete.unwrap_or(false);

    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let result = client.execute(
        "INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete) VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (id) DO UPDATE SET pet_id = $2, quantity = $3, ship_date = $4, status = $5, complete = $6",
        &[&id, &order.pet_id, &order.quantity, &order.ship_date, &status, &complete],
    ).await;

    match result {
        Ok(_) => {
            order.status = Some(status);
            order.complete = Some(complete);
            HttpResponse::Ok().json(&order)
        }
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn get_order_by_id(data: web::Data<AppState>, path: web::Path<i64>) -> HttpResponse {
    let order_id = path.into_inner();
    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let result = client.query_opt(
        "SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id = $1",
        &[&order_id],
    ).await;

    match result {
        Ok(Some(row)) => {
            let order = Order {
                id: Some(row.get(0)),
                pet_id: row.get(1),
                quantity: row.get(2),
                ship_date: row.get(3),
                status: row.get(4),
                complete: row.get(5),
            };
            HttpResponse::Ok().json(&order)
        }
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn delete_order(data: web::Data<AppState>, path: web::Path<i64>) -> HttpResponse {
    let order_id = path.into_inner();
    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let result = client.execute("DELETE FROM orders WHERE id = $1", &[&order_id]).await;

    match result {
        Ok(0) => HttpResponse::NotFound().finish(),
        Ok(_) => HttpResponse::Ok().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

// --- User Handlers ---

async fn create_user(data: web::Data<AppState>, body: web::Json<User>) -> HttpResponse {
    let mut user = body.into_inner();
    let id = user.id.unwrap_or_else(|| data.user_id_counter.fetch_add(1, Ordering::Relaxed) + 1);
    loop {
        let current = data.user_id_counter.load(Ordering::Relaxed);
        if id <= current { break; }
        if data.user_id_counter.compare_exchange(current, id, Ordering::Relaxed, Ordering::Relaxed).is_ok() { break; }
    }
    user.id = Some(id);
    let user_status = user.user_status.unwrap_or(0);

    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let result = client.execute(
        "INSERT INTO users (id, username, first_name, last_name, email, password, phone, user_status) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT (id) DO UPDATE SET username = $2, first_name = $3, last_name = $4, email = $5, password = $6, phone = $7, user_status = $8",
        &[&id, &user.username, &user.first_name, &user.last_name, &user.email, &user.password, &user.phone, &user_status],
    ).await;

    match result {
        Ok(_) => {
            user.user_status = Some(user_status);
            HttpResponse::Ok().json(&user)
        }
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn get_user_by_name(data: web::Data<AppState>, path: web::Path<String>) -> HttpResponse {
    let username = path.into_inner();
    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let result = client.query_opt(
        "SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username = $1",
        &[&username],
    ).await;

    match result {
        Ok(Some(row)) => {
            let user = User {
                id: Some(row.get(0)),
                username: row.get(1),
                first_name: row.get(2),
                last_name: row.get(3),
                email: row.get(4),
                password: row.get(5),
                phone: row.get(6),
                user_status: row.get(7),
            };
            HttpResponse::Ok().json(&user)
        }
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn update_user(data: web::Data<AppState>, path: web::Path<String>, body: web::Json<User>) -> HttpResponse {
    let username = path.into_inner();
    let user = body.into_inner();

    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    // Check if user exists
    let existing = client.query_opt(
        "SELECT id FROM users WHERE username = $1",
        &[&username],
    ).await;

    match existing {
        Ok(Some(row)) => {
            let existing_id: i64 = row.get(0);
            let id = user.id.unwrap_or(existing_id);
            let new_username = user.username.clone().unwrap_or_else(|| username.clone());
            let user_status = user.user_status.unwrap_or(0);

            let result = client.execute(
                "UPDATE users SET id = $1, username = $2, first_name = $3, last_name = $4, email = $5, password = $6, phone = $7, user_status = $8 WHERE username = $9",
                &[&id, &new_username, &user.first_name, &user.last_name, &user.email, &user.password, &user.phone, &user_status, &username],
            ).await;

            match result {
                Ok(_) => {
                    let mut u = user;
                    u.id = Some(id);
                    u.username = Some(new_username);
                    u.user_status = Some(user_status);
                    HttpResponse::Ok().json(&u)
                }
                Err(_) => HttpResponse::InternalServerError().finish(),
            }
        }
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn delete_user(data: web::Data<AppState>, path: web::Path<String>) -> HttpResponse {
    let username = path.into_inner();
    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let result = client.execute("DELETE FROM users WHERE username = $1", &[&username]).await;

    match result {
        Ok(0) => HttpResponse::NotFound().finish(),
        Ok(_) => HttpResponse::Ok().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn login_user(data: web::Data<AppState>, query: web::Query<LoginQuery>) -> HttpResponse {
    let client = match data.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let result = client.query_opt(
        "SELECT username FROM users WHERE username = $1 AND password = $2",
        &[&query.username, &query.password],
    ).await;

    match result {
        Ok(Some(_)) => HttpResponse::Ok().json("logged in"),
        Ok(None) => HttpResponse::BadRequest().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

// --- Main ---

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

    init_db(&pool).await.expect("Failed to initialize database");
    let (pet_max, order_max, user_max) = init_id_counters(&pool).await.expect("Failed to init counters");

    let app_state = web::Data::new(AppState {
        pool,
        pet_id_counter: AtomicI64::new(pet_max),
        order_id_counter: AtomicI64::new(order_max),
        user_id_counter: AtomicI64::new(user_max),
    });

    HttpServer::new(move || {
        App::new()
            .app_data(app_state.clone())
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
