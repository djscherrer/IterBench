use actix_web::middleware::Logger;
use actix_web::{web, App, HttpResponse, HttpServer, Responder};
use chrono::{DateTime, Utc};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use serde::{Deserialize, Serialize};
use std::env;
use tokio_postgres::{Config, NoTls, Row};

#[derive(Clone)]
struct AppState {
    pool: Pool,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(rename_all = "lowercase")]
enum PetStatus {
    Available,
    Pending,
    Sold,
}

impl PetStatus {
    fn as_str(&self) -> &'static str {
        match self {
            PetStatus::Available => "available",
            PetStatus::Pending => "pending",
            PetStatus::Sold => "sold",
        }
    }

    fn from_str(s: &str) -> Option<Self> {
        match s {
            "available" => Some(PetStatus::Available),
            "pending" => Some(PetStatus::Pending),
            "sold" => Some(PetStatus::Sold),
            _ => None,
        }
    }
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct Pet {
    #[serde(skip_serializing_if = "Option::is_none")]
    id: Option<i64>,
    name: String,
    #[serde(rename = "photoUrls")]
    photo_urls: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    status: Option<PetStatus>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(rename_all = "lowercase")]
enum OrderStatus {
    Placed,
    Approved,
    Delivered,
}

impl OrderStatus {
    fn as_str(&self) -> &'static str {
        match self {
            OrderStatus::Placed => "placed",
            OrderStatus::Approved => "approved",
            OrderStatus::Delivered => "delivered",
        }
    }

    fn from_str(s: &str) -> Option<Self> {
        match s {
            "placed" => Some(OrderStatus::Placed),
            "approved" => Some(OrderStatus::Approved),
            "delivered" => Some(OrderStatus::Delivered),
            _ => None,
        }
    }
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
    status: Option<OrderStatus>,
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

#[derive(Deserialize)]
struct StatusQuery {
    status: String,
}

#[derive(Deserialize)]
struct LoginQuery {
    username: String,
    password: String,
}

fn pet_from_row(row: &Row) -> Pet {
    let status: Option<String> = row.get("status");
    Pet {
        id: Some(row.get("id")),
        name: row.get("name"),
        photo_urls: row.get("photo_urls"),
        status: status.as_deref().and_then(PetStatus::from_str),
    }
}

fn order_from_row(row: &Row) -> Order {
    let status: Option<String> = row.get("status");
    Order {
        id: Some(row.get("id")),
        pet_id: row.get("pet_id"),
        quantity: row.get("quantity"),
        ship_date: row.get("ship_date"),
        status: status.as_deref().and_then(OrderStatus::from_str),
        complete: row.get("complete"),
    }
}

fn user_from_row(row: &Row) -> User {
    User {
        id: Some(row.get("id")),
        username: row.get("username"),
        first_name: row.get("first_name"),
        last_name: row.get("last_name"),
        email: row.get("email"),
        password: row.get("password"),
        phone: row.get("phone"),
        user_status: row.get("user_status"),
    }
}

async fn init_db(pool: &Pool) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let client = pool.get().await?;

    client
        .batch_execute(
            "
            CREATE TABLE IF NOT EXISTS pets (
                id BIGINT PRIMARY KEY,
                name TEXT NOT NULL,
                photo_urls TEXT[] NOT NULL,
                status TEXT NULL CHECK (status IN ('available', 'pending', 'sold'))
            );

            CREATE TABLE IF NOT EXISTS orders (
                id BIGINT PRIMARY KEY,
                pet_id BIGINT NULL,
                quantity INTEGER NULL,
                ship_date TIMESTAMPTZ NULL,
                status TEXT NULL CHECK (status IN ('placed', 'approved', 'delivered')),
                complete BOOLEAN NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id BIGINT PRIMARY KEY,
                username TEXT UNIQUE NULL,
                first_name TEXT NULL,
                last_name TEXT NULL,
                email TEXT NULL,
                password TEXT NULL,
                phone TEXT NULL,
                user_status INTEGER NULL
            );
            ",
        )
        .await?;

    Ok(())
}

fn bad_request(message: &str) -> HttpResponse {
    HttpResponse::BadRequest().body(message.to_string())
}

fn not_found(message: &str) -> HttpResponse {
    HttpResponse::NotFound().body(message.to_string())
}

async fn add_pet(state: web::Data<AppState>, body: web::Json<Pet>) -> impl Responder {
    let pet = body.into_inner();

    if pet.name.trim().is_empty() || pet.photo_urls.is_empty() {
        return bad_request("Invalid input");
    }

    let id = pet.id.unwrap_or_else(|| Utc::now().timestamp_millis());
    let status = pet.status.as_ref().map(|s| s.as_str().to_string());

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let row = match client
        .query_one(
            "
            INSERT INTO pets (id, name, photo_urls, status)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                photo_urls = EXCLUDED.photo_urls,
                status = EXCLUDED.status
            RETURNING id, name, photo_urls, status
            ",
            &[&id, &pet.name, &pet.photo_urls, &status],
        )
        .await
    {
        Ok(row) => row,
        Err(_) => return bad_request("Invalid input"),
    };

    HttpResponse::Ok().json(pet_from_row(&row))
}

async fn update_pet(state: web::Data<AppState>, body: web::Json<Pet>) -> impl Responder {
    let pet = body.into_inner();

    if pet.name.trim().is_empty() || pet.photo_urls.is_empty() {
        return bad_request("Invalid input");
    }

    let id = match pet.id {
        Some(id) => id,
        None => return not_found("Pet not found"),
    };

    let status = pet.status.as_ref().map(|s| s.as_str().to_string());

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let updated = match client
        .query_opt(
            "
            UPDATE pets
            SET name = $2, photo_urls = $3, status = $4
            WHERE id = $1
            RETURNING id, name, photo_urls, status
            ",
            &[&id, &pet.name, &pet.photo_urls, &status],
        )
        .await
    {
        Ok(row) => row,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match updated {
        Some(row) => HttpResponse::Ok().json(pet_from_row(&row)),
        None => not_found("Pet not found"),
    }
}

async fn find_pets_by_status(
    state: web::Data<AppState>,
    query: web::Query<StatusQuery>,
) -> impl Responder {
    if PetStatus::from_str(&query.status).is_none() {
        return bad_request("Invalid status");
    }

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let rows = match client
        .query(
            "SELECT id, name, photo_urls, status FROM pets WHERE status = $1 ORDER BY id",
            &[&query.status],
        )
        .await
    {
        Ok(rows) => rows,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let pets: Vec<Pet> = rows.iter().map(pet_from_row).collect();
    HttpResponse::Ok().json(pets)
}

async fn get_pet_by_id(state: web::Data<AppState>, path: web::Path<i64>) -> impl Responder {
    let pet_id = path.into_inner();

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let row = match client
        .query_opt(
            "SELECT id, name, photo_urls, status FROM pets WHERE id = $1",
            &[&pet_id],
        )
        .await
    {
        Ok(row) => row,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match row {
        Some(row) => HttpResponse::Ok().json(pet_from_row(&row)),
        None => not_found("Pet not found"),
    }
}

async fn delete_pet(state: web::Data<AppState>, path: web::Path<i64>) -> impl Responder {
    let pet_id = path.into_inner();

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let deleted = match client
        .execute("DELETE FROM pets WHERE id = $1", &[&pet_id])
        .await
    {
        Ok(count) => count,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    if deleted == 0 {
        not_found("Pet not found")
    } else {
        HttpResponse::Ok().finish()
    }
}

async fn place_order(state: web::Data<AppState>, body: web::Json<Order>) -> impl Responder {
    let order = body.into_inner();
    let id = order.id.unwrap_or_else(|| Utc::now().timestamp_millis());
    let status = order.status.as_ref().map(|s| s.as_str().to_string());

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let row = match client
        .query_one(
            "
            INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (id) DO UPDATE SET
                pet_id = EXCLUDED.pet_id,
                quantity = EXCLUDED.quantity,
                ship_date = EXCLUDED.ship_date,
                status = EXCLUDED.status,
                complete = EXCLUDED.complete
            RETURNING id, pet_id, quantity, ship_date, status, complete
            ",
            &[
                &id,
                &order.pet_id,
                &order.quantity,
                &order.ship_date,
                &status,
                &order.complete,
            ],
        )
        .await
    {
        Ok(row) => row,
        Err(_) => return bad_request("Invalid input"),
    };

    HttpResponse::Ok().json(order_from_row(&row))
}

async fn get_order_by_id(state: web::Data<AppState>, path: web::Path<i64>) -> impl Responder {
    let order_id = path.into_inner();

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let row = match client
        .query_opt(
            "SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id = $1",
            &[&order_id],
        )
        .await
    {
        Ok(row) => row,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match row {
        Some(row) => HttpResponse::Ok().json(order_from_row(&row)),
        None => not_found("Order not found"),
    }
}

async fn delete_order(state: web::Data<AppState>, path: web::Path<i64>) -> impl Responder {
    let order_id = path.into_inner();

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let deleted = match client
        .execute("DELETE FROM orders WHERE id = $1", &[&order_id])
        .await
    {
        Ok(count) => count,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    if deleted == 0 {
        not_found("Order not found")
    } else {
        HttpResponse::Ok().finish()
    }
}

async fn create_user(state: web::Data<AppState>, body: web::Json<User>) -> impl Responder {
    let user = body.into_inner();
    let id = user.id.unwrap_or_else(|| Utc::now().timestamp_millis());

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let row = match client
        .query_one(
            "
            INSERT INTO users (
                id, username, first_name, last_name, email, password, phone, user_status
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (id) DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                email = EXCLUDED.email,
                password = EXCLUDED.password,
                phone = EXCLUDED.phone,
                user_status = EXCLUDED.user_status
            RETURNING id, username, first_name, last_name, email, password, phone, user_status
            ",
            &[
                &id,
                &user.username,
                &user.first_name,
                &user.last_name,
                &user.email,
                &user.password,
                &user.phone,
                &user.user_status,
            ],
        )
        .await
    {
        Ok(row) => row,
        Err(_) => return bad_request("Invalid input"),
    };

    HttpResponse::Ok().json(user_from_row(&row))
}

async fn get_user_by_name(state: web::Data<AppState>, path: web::Path<String>) -> impl Responder {
    let username = path.into_inner();

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let row = match client
        .query_opt(
            "
            SELECT id, username, first_name, last_name, email, password, phone, user_status
            FROM users WHERE username = $1
            ",
            &[&username],
        )
        .await
    {
        Ok(row) => row,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match row {
        Some(row) => HttpResponse::Ok().json(user_from_row(&row)),
        None => not_found("User not found"),
    }
}

async fn update_user(
    state: web::Data<AppState>,
    path: web::Path<String>,
    body: web::Json<User>,
) -> impl Responder {
    let username = path.into_inner();
    let user = body.into_inner();

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let row = match client
        .query_opt(
            "
            UPDATE users
            SET
                username = $2,
                first_name = $3,
                last_name = $4,
                email = $5,
                password = $6,
                phone = $7,
                user_status = $8
            WHERE username = $1
            RETURNING id, username, first_name, last_name, email, password, phone, user_status
            ",
            &[
                &username,
                &user.username,
                &user.first_name,
                &user.last_name,
                &user.email,
                &user.password,
                &user.phone,
                &user.user_status,
            ],
        )
        .await
    {
        Ok(row) => row,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match row {
        Some(row) => HttpResponse::Ok().json(user_from_row(&row)),
        None => not_found("User not found"),
    }
}

async fn delete_user(state: web::Data<AppState>, path: web::Path<String>) -> impl Responder {
    let username = path.into_inner();

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let deleted = match client
        .execute("DELETE FROM users WHERE username = $1", &[&username])
        .await
    {
        Ok(count) => count,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    if deleted == 0 {
        not_found("User not found")
    } else {
        HttpResponse::Ok().finish()
    }
}

async fn login_user(state: web::Data<AppState>, query: web::Query<LoginQuery>) -> impl Responder {
    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let row = match client
        .query_opt(
            "SELECT password FROM users WHERE username = $1",
            &[&query.username],
        )
        .await
    {
        Ok(row) => row,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match row {
        Some(row) => {
            let stored_password: Option<String> = row.get("password");
            if stored_password.as_deref() == Some(query.password.as_str()) {
                HttpResponse::Ok().json(format!("User {} logged in successfully", query.username))
            } else {
                bad_request("Invalid credentials")
            }
        }
        None => bad_request("Invalid credentials"),
    }
}

fn build_pool_from_env() -> Result<Pool, Box<dyn std::error::Error + Send + Sync>> {
    let db_host = env::var("DB_HOST")?;
    let db_port: u16 = env::var("DB_PORT")?.parse()?;
    let db_user = env::var("DB_USER")?;
    let db_password = env::var("DB_PASSWORD")?;
    let db_name = env::var("DB_NAME")?;

    let mut cfg = Config::new();
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
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let pool = build_pool_from_env().map_err(|e| {
        std::io::Error::new(std::io::ErrorKind::Other, format!("database config error: {}", e))
    })?;

    init_db(&pool).await.map_err(|e| {
        std::io::Error::new(
            std::io::ErrorKind::Other,
            format!("database initialization error: {}", e),
        )
    })?;

    let state = AppState { pool };

    let port: u16 = env::var("PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(5001);

    HttpServer::new(move || {
        App::new()
            .wrap(Logger::default())
            .app_data(web::Data::new(state.clone()))
            .route("/pet", web::post().to(add_pet))
            .route("/pet", web::put().to(update_pet))
            .route("/pet/findByStatus", web::get().to(find_pets_by_status))
            .route("/pet/{petId}", web::get().to(get_pet_by_id))
            .route("/pet/{petId}", web::delete().to(delete_pet))
            .route("/store/order", web::post().to(place_order))
            .route("/store/order/{orderId}", web::get().to(get_order_by_id))
            .route("/store/order/{orderId}", web::delete().to(delete_order))
            .route("/user", web::post().to(create_user))
            .route("/user/{username}", web::get().to(get_user_by_name))
            .route("/user/{username}", web::put().to(update_user))
            .route("/user/{username}", web::delete().to(delete_user))
            .route("/user/login", web::get().to(login_user))
    })
    .bind(("0.0.0.0", port))?
    .run()
    .await
}