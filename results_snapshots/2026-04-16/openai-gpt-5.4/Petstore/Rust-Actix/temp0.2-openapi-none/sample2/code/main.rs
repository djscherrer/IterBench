use actix_web::{
    middleware::Logger,
    web, App, HttpResponse, HttpServer, Responder,
};
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

    fn from_str(value: &str) -> Option<Self> {
        match value {
            "available" => Some(PetStatus::Available),
            "pending" => Some(PetStatus::Pending),
            "sold" => Some(PetStatus::Sold),
            _ => None,
        }
    }
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct Pet {
    id: Option<i64>,
    name: String,
    #[serde(rename = "photoUrls")]
    photo_urls: Vec<String>,
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

    fn from_str(value: &str) -> Option<Self> {
        match value {
            "placed" => Some(OrderStatus::Placed),
            "approved" => Some(OrderStatus::Approved),
            "delivered" => Some(OrderStatus::Delivered),
            _ => None,
        }
    }
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct Order {
    id: Option<i64>,
    #[serde(rename = "petId")]
    pet_id: Option<i64>,
    quantity: Option<i32>,
    #[serde(rename = "shipDate")]
    ship_date: Option<DateTime<Utc>>,
    status: Option<OrderStatus>,
    complete: Option<bool>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct User {
    id: Option<i64>,
    username: Option<String>,
    #[serde(rename = "firstName")]
    first_name: Option<String>,
    #[serde(rename = "lastName")]
    last_name: Option<String>,
    email: Option<String>,
    password: Option<String>,
    phone: Option<String>,
    #[serde(rename = "userStatus")]
    user_status: Option<i32>,
}

#[derive(Deserialize)]
struct FindPetsByStatusQuery {
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
        id: row.get("id"),
        name: row.get("name"),
        photo_urls: row.get("photo_urls"),
        status: status.as_deref().and_then(PetStatus::from_str),
    }
}

fn order_from_row(row: &Row) -> Order {
    let status: Option<String> = row.get("status");
    Order {
        id: row.get("id"),
        pet_id: row.get("pet_id"),
        quantity: row.get("quantity"),
        ship_date: row.get("ship_date"),
        status: status.as_deref().and_then(OrderStatus::from_str),
        complete: row.get("complete"),
    }
}

fn user_from_row(row: &Row) -> User {
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

async fn init_db(pool: &Pool) -> Result<(), String> {
    let client = pool.get().await.map_err(|e| e.to_string())?;

    client
        .batch_execute(
            r#"
            CREATE TABLE IF NOT EXISTS pets (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                photo_urls TEXT[] NOT NULL,
                status TEXT CHECK (status IN ('available', 'pending', 'sold') OR status IS NULL)
            );

            CREATE TABLE IF NOT EXISTS orders (
                id BIGSERIAL PRIMARY KEY,
                pet_id BIGINT REFERENCES pets(id) ON DELETE SET NULL,
                quantity INTEGER,
                ship_date TIMESTAMPTZ,
                status TEXT CHECK (status IN ('placed', 'approved', 'delivered') OR status IS NULL),
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
            "#,
        )
        .await
        .map_err(|e| e.to_string())?;

    Ok(())
}

async fn add_pet(state: web::Data<AppState>, payload: web::Json<Pet>) -> impl Responder {
    let pet = payload.into_inner();

    if pet.name.trim().is_empty() || pet.photo_urls.is_empty() {
        return HttpResponse::BadRequest().finish();
    }

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let row = if let Some(id) = pet.id {
        client
            .query_one(
                r#"
                INSERT INTO pets (id, name, photo_urls, status)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (id) DO UPDATE
                SET name = EXCLUDED.name,
                    photo_urls = EXCLUDED.photo_urls,
                    status = EXCLUDED.status
                RETURNING id, name, photo_urls, status
                "#,
                &[&id, &pet.name, &pet.photo_urls, &pet.status.as_ref().map(|s| s.as_str())],
            )
            .await
    } else {
        client
            .query_one(
                r#"
                INSERT INTO pets (name, photo_urls, status)
                VALUES ($1, $2, $3)
                RETURNING id, name, photo_urls, status
                "#,
                &[&pet.name, &pet.photo_urls, &pet.status.as_ref().map(|s| s.as_str())],
            )
            .await
    };

    match row {
        Ok(row) => HttpResponse::Ok().json(pet_from_row(&row)),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn update_pet(state: web::Data<AppState>, payload: web::Json<Pet>) -> impl Responder {
    let pet = payload.into_inner();
    let pet_id = match pet.id {
        Some(id) => id,
        None => return HttpResponse::NotFound().finish(),
    };

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query_opt(
            r#"
            UPDATE pets
            SET name = $2, photo_urls = $3, status = $4
            WHERE id = $1
            RETURNING id, name, photo_urls, status
            "#,
            &[&pet_id, &pet.name, &pet.photo_urls, &pet.status.as_ref().map(|s| s.as_str())],
        )
        .await
    {
        Ok(Some(row)) => HttpResponse::Ok().json(pet_from_row(&row)),
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn find_pets_by_status(
    state: web::Data<AppState>,
    query: web::Query<FindPetsByStatusQuery>,
) -> impl Responder {
    if PetStatus::from_str(&query.status).is_none() {
        return HttpResponse::BadRequest().finish();
    }

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query(
            "SELECT id, name, photo_urls, status FROM pets WHERE status = $1 ORDER BY id",
            &[&query.status],
        )
        .await
    {
        Ok(rows) => {
            let pets: Vec<Pet> = rows.iter().map(pet_from_row).collect();
            HttpResponse::Ok().json(pets)
        }
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn get_pet_by_id(state: web::Data<AppState>, path: web::Path<i64>) -> impl Responder {
    let pet_id = path.into_inner();

    let client = match state.pool.get().await {
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
        Ok(Some(row)) => HttpResponse::Ok().json(pet_from_row(&row)),
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn delete_pet(state: web::Data<AppState>, path: web::Path<i64>) -> impl Responder {
    let pet_id = path.into_inner();

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .execute("DELETE FROM pets WHERE id = $1", &[&pet_id])
        .await
    {
        Ok(0) => HttpResponse::NotFound().finish(),
        Ok(_) => HttpResponse::Ok().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn place_order(state: web::Data<AppState>, payload: web::Json<Order>) -> impl Responder {
    let order = payload.into_inner();

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let row = if let Some(id) = order.id {
        client
            .query_one(
                r#"
                INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (id) DO UPDATE
                SET pet_id = EXCLUDED.pet_id,
                    quantity = EXCLUDED.quantity,
                    ship_date = EXCLUDED.ship_date,
                    status = EXCLUDED.status,
                    complete = EXCLUDED.complete
                RETURNING id, pet_id, quantity, ship_date, status, complete
                "#,
                &[
                    &id,
                    &order.pet_id,
                    &order.quantity,
                    &order.ship_date,
                    &order.status.as_ref().map(|s| s.as_str()),
                    &order.complete,
                ],
            )
            .await
    } else {
        client
            .query_one(
                r#"
                INSERT INTO orders (pet_id, quantity, ship_date, status, complete)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, pet_id, quantity, ship_date, status, complete
                "#,
                &[
                    &order.pet_id,
                    &order.quantity,
                    &order.ship_date,
                    &order.status.as_ref().map(|s| s.as_str()),
                    &order.complete,
                ],
            )
            .await
    };

    match row {
        Ok(row) => HttpResponse::Ok().json(order_from_row(&row)),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn get_order_by_id(state: web::Data<AppState>, path: web::Path<i64>) -> impl Responder {
    let order_id = path.into_inner();

    let client = match state.pool.get().await {
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
        Ok(Some(row)) => HttpResponse::Ok().json(order_from_row(&row)),
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn delete_order(state: web::Data<AppState>, path: web::Path<i64>) -> impl Responder {
    let order_id = path.into_inner();

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .execute("DELETE FROM orders WHERE id = $1", &[&order_id])
        .await
    {
        Ok(0) => HttpResponse::NotFound().finish(),
        Ok(_) => HttpResponse::Ok().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn create_user(state: web::Data<AppState>, payload: web::Json<User>) -> impl Responder {
    let user = payload.into_inner();

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let row = if let Some(id) = user.id {
        client
            .query_one(
                r#"
                INSERT INTO users (id, username, first_name, last_name, email, password, phone, user_status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (id) DO UPDATE
                SET username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    email = EXCLUDED.email,
                    password = EXCLUDED.password,
                    phone = EXCLUDED.phone,
                    user_status = EXCLUDED.user_status
                RETURNING id, username, first_name, last_name, email, password, phone, user_status
                "#,
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
    } else {
        client
            .query_one(
                r#"
                INSERT INTO users (username, first_name, last_name, email, password, phone, user_status)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id, username, first_name, last_name, email, password, phone, user_status
                "#,
                &[
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
    };

    match row {
        Ok(row) => HttpResponse::Ok().json(user_from_row(&row)),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn get_user_by_name(state: web::Data<AppState>, path: web::Path<String>) -> impl Responder {
    let username = path.into_inner();

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query_opt(
            r#"
            SELECT id, username, first_name, last_name, email, password, phone, user_status
            FROM users
            WHERE username = $1
            "#,
            &[&username],
        )
        .await
    {
        Ok(Some(row)) => HttpResponse::Ok().json(user_from_row(&row)),
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn update_user(
    state: web::Data<AppState>,
    path: web::Path<String>,
    payload: web::Json<User>,
) -> impl Responder {
    let username = path.into_inner();
    let user = payload.into_inner();

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query_opt(
            r#"
            UPDATE users
            SET username = $2,
                first_name = $3,
                last_name = $4,
                email = $5,
                password = $6,
                phone = $7,
                user_status = $8
            WHERE username = $1
            RETURNING id, username, first_name, last_name, email, password, phone, user_status
            "#,
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
        Ok(Some(row)) => HttpResponse::Ok().json(user_from_row(&row)),
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn delete_user(state: web::Data<AppState>, path: web::Path<String>) -> impl Responder {
    let username = path.into_inner();

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .execute("DELETE FROM users WHERE username = $1", &[&username])
        .await
    {
        Ok(0) => HttpResponse::NotFound().finish(),
        Ok(_) => HttpResponse::Ok().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn login_user(
    state: web::Data<AppState>,
    query: web::Query<LoginQuery>,
) -> impl Responder {
    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query_opt(
            "SELECT username FROM users WHERE username = $1 AND password = $2",
            &[&query.username, &query.password],
        )
        .await
    {
        Ok(Some(_)) => HttpResponse::Ok().json("logged in user session"),
        Ok(None) => HttpResponse::BadRequest().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

fn build_pg_config() -> Result<Config, String> {
    let host = env::var("DB_HOST").map_err(|_| "DB_HOST is not set".to_string())?;
    let port: u16 = env::var("DB_PORT")
        .map_err(|_| "DB_PORT is not set".to_string())?
        .parse()
        .map_err(|_| "DB_PORT is invalid".to_string())?;
    let user = env::var("DB_USER").map_err(|_| "DB_USER is not set".to_string())?;
    let password = env::var("DB_PASSWORD").map_err(|_| "DB_PASSWORD is not set".to_string())?;
    let dbname = env::var("DB_NAME").map_err(|_| "DB_NAME is not set".to_string())?;

    let mut cfg = Config::new();
    cfg.host(&host);
    cfg.port(port);
    cfg.user(&user);
    cfg.password(&password);
    cfg.dbname(&dbname);
    Ok(cfg)
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let pg_cfg = build_pg_config().map_err(|e| {
        std::io::Error::new(std::io::ErrorKind::Other, format!("database config error: {}", e))
    })?;

    let mgr_config = ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    };
    let manager = Manager::from_config(pg_cfg, NoTls, mgr_config);
    let pool = Pool::builder(manager)
        .max_size(16)
        .build()
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, format!("pool error: {}", e)))?;

    init_db(&pool).await.map_err(|e| {
        std::io::Error::new(std::io::ErrorKind::Other, format!("database init error: {}", e))
    })?;

    let state = AppState { pool };
    let port: u16 = env::var("PORT")
        .ok()
        .and_then(|p| p.parse::<u16>().ok())
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