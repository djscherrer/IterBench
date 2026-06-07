use actix_web::{
    error::{ErrorBadRequest, ErrorInternalServerError},
    middleware::Logger,
    web, App, HttpResponse, HttpServer, Result,
};
use chrono::{DateTime, Utc};
use deadpool_postgres::{Config as PoolConfig, ManagerConfig, Pool, RecyclingMethod, Runtime};
use serde::{Deserialize, Serialize};
use std::{
    env, io,
    str::FromStr,
    sync::atomic::{AtomicBool, Ordering},
    thread,
};
use tokio::sync::Mutex;
use tokio_postgres::{types::ToSql, NoTls, Row};

struct AppState {
    pool: Pool,
    schema_ready: AtomicBool,
    init_lock: Mutex<()>,
}

type ApiResult = Result<HttpResponse>;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
enum PetStatus {
    Available,
    Pending,
    Sold,
}

impl PetStatus {
    fn as_str(&self) -> &'static str {
        match self {
            Self::Available => "available",
            Self::Pending => "pending",
            Self::Sold => "sold",
        }
    }
}

impl FromStr for PetStatus {
    type Err = ();

    fn from_str(value: &str) -> std::result::Result<Self, Self::Err> {
        match value {
            "available" => Ok(Self::Available),
            "pending" => Ok(Self::Pending),
            "sold" => Ok(Self::Sold),
            _ => Err(()),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
enum OrderStatus {
    Placed,
    Approved,
    Delivered,
}

impl OrderStatus {
    fn as_str(&self) -> &'static str {
        match self {
            Self::Placed => "placed",
            Self::Approved => "approved",
            Self::Delivered => "delivered",
        }
    }
}

impl FromStr for OrderStatus {
    type Err = ();

    fn from_str(value: &str) -> std::result::Result<Self, Self::Err> {
        match value {
            "placed" => Ok(Self::Placed),
            "approved" => Ok(Self::Approved),
            "delivered" => Ok(Self::Delivered),
            _ => Err(()),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Pet {
    #[serde(skip_serializing_if = "Option::is_none")]
    id: Option<i64>,
    name: String,
    photo_urls: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    status: Option<PetStatus>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Order {
    #[serde(skip_serializing_if = "Option::is_none")]
    id: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pet_id: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    quantity: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    ship_date: Option<DateTime<Utc>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    status: Option<OrderStatus>,
    #[serde(skip_serializing_if = "Option::is_none")]
    complete: Option<bool>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct User {
    #[serde(skip_serializing_if = "Option::is_none")]
    id: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    username: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    first_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    last_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    email: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    password: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    phone: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
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

fn to_io_error<E: std::fmt::Display>(error: E) -> io::Error {
    io::Error::new(io::ErrorKind::Other, error.to_string())
}

fn db_error<E: std::fmt::Display>(error: E) -> actix_web::Error {
    ErrorInternalServerError(error.to_string())
}

fn required_env(name: &str) -> io::Result<String> {
    env::var(name).map_err(|_| io::Error::new(io::ErrorKind::NotFound, format!("missing {name}")))
}

fn pool_from_env() -> io::Result<Pool> {
    let mut cfg = PoolConfig::new();
    cfg.host = Some(required_env("DB_HOST")?);
    cfg.port = Some(
        env::var("DB_PORT")
            .ok()
            .and_then(|value| value.parse::<u16>().ok())
            .unwrap_or(5432),
    );
    cfg.user = Some(required_env("DB_USER")?);
    cfg.password = Some(required_env("DB_PASSWORD")?);
    cfg.dbname = Some(required_env("DB_NAME")?);
    cfg.manager = Some(ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    });
    cfg.create_pool(Some(Runtime::Tokio1), NoTls)
        .map_err(to_io_error)
}

async fn init_db(pool: &Pool) -> io::Result<()> {
    let client = pool.get().await.map_err(to_io_error)?;
    client
        .batch_execute(
            r#"
            CREATE TABLE IF NOT EXISTS pets (
                id BIGINT PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
                name TEXT NOT NULL,
                photo_urls TEXT[] NOT NULL,
                status TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_pets_status ON pets (status);

            CREATE TABLE IF NOT EXISTS orders (
                id BIGINT PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
                pet_id BIGINT,
                quantity INTEGER,
                ship_date TIMESTAMPTZ,
                status TEXT,
                complete BOOLEAN
            );
            CREATE INDEX IF NOT EXISTS idx_orders_pet_id ON orders (pet_id);

            CREATE TABLE IF NOT EXISTS users (
                id BIGINT PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
                username TEXT NOT NULL UNIQUE,
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
        .map_err(to_io_error)
}

async fn ensure_db_ready(state: &AppState) -> Result<()> {
    if state.schema_ready.load(Ordering::Acquire) {
        return Ok(());
    }

    let _guard = state.init_lock.lock().await;
    if state.schema_ready.load(Ordering::Acquire) {
        return Ok(());
    }

    init_db(&state.pool).await.map_err(db_error)?;
    state.schema_ready.store(true, Ordering::Release);
    Ok(())
}

fn pet_from_row(row: Row) -> Pet {
    let status = row
        .get::<_, Option<String>>("status")
        .and_then(|value| PetStatus::from_str(&value).ok());

    Pet {
        id: Some(row.get("id")),
        name: row.get("name"),
        photo_urls: row.get("photo_urls"),
        status,
    }
}

fn order_from_row(row: Row) -> Order {
    let status = row
        .get::<_, Option<String>>("status")
        .and_then(|value| OrderStatus::from_str(&value).ok());

    Order {
        id: Some(row.get("id")),
        pet_id: row.get("pet_id"),
        quantity: row.get("quantity"),
        ship_date: row.get("ship_date"),
        status,
        complete: row.get("complete"),
    }
}

fn user_from_row(row: Row) -> User {
    User {
        id: Some(row.get("id")),
        username: Some(row.get("username")),
        first_name: row.get("first_name"),
        last_name: row.get("last_name"),
        email: row.get("email"),
        password: row.get("password"),
        phone: row.get("phone"),
        user_status: row.get("user_status"),
    }
}

async fn add_pet(state: web::Data<AppState>, payload: web::Json<Pet>) -> ApiResult {
    ensure_db_ready(state.get_ref()).await?;
    let pet = payload.into_inner();
    let status = pet.status.as_ref().map(PetStatus::as_str);
    let client = state.pool.get().await.map_err(db_error)?;

    let row = match pet.id {
        Some(id) => client
            .query_one(
                "INSERT INTO pets (id, name, photo_urls, status) VALUES ($1, $2, $3, $4)
                 ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, photo_urls = EXCLUDED.photo_urls, status = EXCLUDED.status
                 RETURNING id, name, photo_urls, status",
                &[&id, &pet.name, &pet.photo_urls, &status],
            )
            .await
            .map_err(db_error)?,
        None => client
            .query_one(
                "INSERT INTO pets (name, photo_urls, status) VALUES ($1, $2, $3)
                 RETURNING id, name, photo_urls, status",
                &[&pet.name, &pet.photo_urls, &status],
            )
            .await
            .map_err(db_error)?,
    };

    Ok(HttpResponse::Ok().json(pet_from_row(row)))
}

async fn update_pet(state: web::Data<AppState>, payload: web::Json<Pet>) -> ApiResult {
    ensure_db_ready(state.get_ref()).await?;
    let pet = payload.into_inner();
    let Some(id) = pet.id else {
        return Ok(HttpResponse::NotFound().finish());
    };

    let status = pet.status.as_ref().map(PetStatus::as_str);
    let client = state.pool.get().await.map_err(db_error)?;
    let updated = client
        .query_opt(
            "UPDATE pets
             SET name = $2, photo_urls = $3, status = $4
             WHERE id = $1
             RETURNING id, name, photo_urls, status",
            &[&id, &pet.name, &pet.photo_urls, &status],
        )
        .await
        .map_err(db_error)?;

    match updated {
        Some(row) => Ok(HttpResponse::Ok().json(pet_from_row(row))),
        None => Ok(HttpResponse::NotFound().finish()),
    }
}

async fn find_pets_by_status(
    state: web::Data<AppState>,
    query: web::Query<StatusQuery>,
) -> ApiResult {
    ensure_db_ready(state.get_ref()).await?;
    let status = PetStatus::from_str(&query.status).map_err(|_| ErrorBadRequest("invalid status"))?;
    let status_str = status.as_str();
    let client = state.pool.get().await.map_err(db_error)?;
    let rows = client
        .query(
            "SELECT id, name, photo_urls, status FROM pets WHERE status = $1 ORDER BY id",
            &[&status_str],
        )
        .await
        .map_err(db_error)?;

    let pets: Vec<Pet> = rows.into_iter().map(pet_from_row).collect();
    Ok(HttpResponse::Ok().json(pets))
}

async fn get_pet_by_id(state: web::Data<AppState>, pet_id: web::Path<i64>) -> ApiResult {
    ensure_db_ready(state.get_ref()).await?;
    let client = state.pool.get().await.map_err(db_error)?;
    let pet = client
        .query_opt(
            "SELECT id, name, photo_urls, status FROM pets WHERE id = $1",
            &[&pet_id.into_inner()],
        )
        .await
        .map_err(db_error)?;

    match pet {
        Some(row) => Ok(HttpResponse::Ok().json(pet_from_row(row))),
        None => Ok(HttpResponse::NotFound().finish()),
    }
}

async fn delete_pet(state: web::Data<AppState>, pet_id: web::Path<i64>) -> ApiResult {
    ensure_db_ready(state.get_ref()).await?;
    let client = state.pool.get().await.map_err(db_error)?;
    let deleted = client
        .execute("DELETE FROM pets WHERE id = $1", &[&pet_id.into_inner()])
        .await
        .map_err(db_error)?;

    if deleted == 0 {
        Ok(HttpResponse::NotFound().finish())
    } else {
        Ok(HttpResponse::Ok().finish())
    }
}

async fn place_order(state: web::Data<AppState>, payload: web::Json<Order>) -> ApiResult {
    ensure_db_ready(state.get_ref()).await?;
    let order = payload.into_inner();
    let status = order.status.as_ref().map(OrderStatus::as_str);
    let client = state.pool.get().await.map_err(db_error)?;

    let params_without_id: [&(dyn ToSql + Sync); 5] = [
        &order.pet_id,
        &order.quantity,
        &order.ship_date,
        &status,
        &order.complete,
    ];

    let row = match order.id {
        Some(id) => client
            .query_one(
                "INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete)
                 VALUES ($1, $2, $3, $4, $5, $6)
                 ON CONFLICT (id) DO UPDATE
                 SET pet_id = EXCLUDED.pet_id,
                     quantity = EXCLUDED.quantity,
                     ship_date = EXCLUDED.ship_date,
                     status = EXCLUDED.status,
                     complete = EXCLUDED.complete
                 RETURNING id, pet_id, quantity, ship_date, status, complete",
                &[&id, &order.pet_id, &order.quantity, &order.ship_date, &status, &order.complete],
            )
            .await
            .map_err(db_error)?,
        None => client
            .query_one(
                "INSERT INTO orders (pet_id, quantity, ship_date, status, complete)
                 VALUES ($1, $2, $3, $4, $5)
                 RETURNING id, pet_id, quantity, ship_date, status, complete",
                &params_without_id,
            )
            .await
            .map_err(db_error)?,
    };

    Ok(HttpResponse::Ok().json(order_from_row(row)))
}

async fn get_order_by_id(state: web::Data<AppState>, order_id: web::Path<i64>) -> ApiResult {
    ensure_db_ready(state.get_ref()).await?;
    let client = state.pool.get().await.map_err(db_error)?;
    let order = client
        .query_opt(
            "SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id = $1",
            &[&order_id.into_inner()],
        )
        .await
        .map_err(db_error)?;

    match order {
        Some(row) => Ok(HttpResponse::Ok().json(order_from_row(row))),
        None => Ok(HttpResponse::NotFound().finish()),
    }
}

async fn delete_order(state: web::Data<AppState>, order_id: web::Path<i64>) -> ApiResult {
    ensure_db_ready(state.get_ref()).await?;
    let client = state.pool.get().await.map_err(db_error)?;
    let deleted = client
        .execute("DELETE FROM orders WHERE id = $1", &[&order_id.into_inner()])
        .await
        .map_err(db_error)?;

    if deleted == 0 {
        Ok(HttpResponse::NotFound().finish())
    } else {
        Ok(HttpResponse::Ok().finish())
    }
}

fn validated_username(username: Option<String>) -> Result<String> {
    match username {
        Some(value) if !value.trim().is_empty() => Ok(value),
        _ => Err(ErrorBadRequest("username is required")),
    }
}

async fn create_user(state: web::Data<AppState>, payload: web::Json<User>) -> ApiResult {
    ensure_db_ready(state.get_ref()).await?;
    let user = payload.into_inner();
    let username = validated_username(user.username.clone())?;
    let client = state.pool.get().await.map_err(db_error)?;

    let row = match user.id {
        Some(id) => client
            .query_one(
                "INSERT INTO users (id, username, first_name, last_name, email, password, phone, user_status)
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                 ON CONFLICT (username) DO UPDATE
                 SET first_name = EXCLUDED.first_name,
                     last_name = EXCLUDED.last_name,
                     email = EXCLUDED.email,
                     password = EXCLUDED.password,
                     phone = EXCLUDED.phone,
                     user_status = EXCLUDED.user_status
                 RETURNING id, username, first_name, last_name, email, password, phone, user_status",
                &[
                    &id,
                    &username,
                    &user.first_name,
                    &user.last_name,
                    &user.email,
                    &user.password,
                    &user.phone,
                    &user.user_status,
                ],
            )
            .await
            .map_err(db_error)?,
        None => client
            .query_one(
                "INSERT INTO users (username, first_name, last_name, email, password, phone, user_status)
                 VALUES ($1, $2, $3, $4, $5, $6, $7)
                 ON CONFLICT (username) DO UPDATE
                 SET first_name = EXCLUDED.first_name,
                     last_name = EXCLUDED.last_name,
                     email = EXCLUDED.email,
                     password = EXCLUDED.password,
                     phone = EXCLUDED.phone,
                     user_status = EXCLUDED.user_status
                 RETURNING id, username, first_name, last_name, email, password, phone, user_status",
                &[
                    &username,
                    &user.first_name,
                    &user.last_name,
                    &user.email,
                    &user.password,
                    &user.phone,
                    &user.user_status,
                ],
            )
            .await
            .map_err(db_error)?,
    };

    Ok(HttpResponse::Ok().json(user_from_row(row)))
}

async fn get_user_by_name(state: web::Data<AppState>, username: web::Path<String>) -> ApiResult {
    ensure_db_ready(state.get_ref()).await?;
    let client = state.pool.get().await.map_err(db_error)?;
    let user = client
        .query_opt(
            "SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username = $1",
            &[&username.into_inner()],
        )
        .await
        .map_err(db_error)?;

    match user {
        Some(row) => Ok(HttpResponse::Ok().json(user_from_row(row))),
        None => Ok(HttpResponse::NotFound().finish()),
    }
}

async fn update_user(
    state: web::Data<AppState>,
    username: web::Path<String>,
    payload: web::Json<User>,
) -> ApiResult {
    ensure_db_ready(state.get_ref()).await?;
    let user = payload.into_inner();
    let current_username = username.into_inner();
    let new_username = match user.username.clone() {
        Some(value) if !value.trim().is_empty() => value,
        _ => current_username.clone(),
    };

    let client = state.pool.get().await.map_err(db_error)?;
    let updated = client
        .query_opt(
            "UPDATE users
             SET id = COALESCE($2, id),
                 username = $3,
                 first_name = $4,
                 last_name = $5,
                 email = $6,
                 password = $7,
                 phone = $8,
                 user_status = $9
             WHERE username = $1
             RETURNING id, username, first_name, last_name, email, password, phone, user_status",
            &[
                &current_username,
                &user.id,
                &new_username,
                &user.first_name,
                &user.last_name,
                &user.email,
                &user.password,
                &user.phone,
                &user.user_status,
            ],
        )
        .await
        .map_err(db_error)?;

    match updated {
        Some(row) => Ok(HttpResponse::Ok().json(user_from_row(row))),
        None => Ok(HttpResponse::NotFound().finish()),
    }
}

async fn delete_user(state: web::Data<AppState>, username: web::Path<String>) -> ApiResult {
    ensure_db_ready(state.get_ref()).await?;
    let client = state.pool.get().await.map_err(db_error)?;
    let deleted = client
        .execute("DELETE FROM users WHERE username = $1", &[&username.into_inner()])
        .await
        .map_err(db_error)?;

    if deleted == 0 {
        Ok(HttpResponse::NotFound().finish())
    } else {
        Ok(HttpResponse::Ok().finish())
    }
}

async fn login_user(state: web::Data<AppState>, query: web::Query<LoginQuery>) -> ApiResult {
    ensure_db_ready(state.get_ref()).await?;
    let client = state.pool.get().await.map_err(db_error)?;
    let row = client
        .query_opt(
            "SELECT password FROM users WHERE username = $1",
            &[&query.username],
        )
        .await
        .map_err(db_error)?;

    match row.and_then(|row| row.get::<_, Option<String>>("password")) {
        Some(password) if password == query.password => {
            Ok(HttpResponse::Ok().json(format!("logged in user: {}", query.username)))
        }
        _ => Ok(HttpResponse::BadRequest().finish()),
    }
}

#[actix_web::main]
async fn main() -> io::Result<()> {
    env_logger::init();

    let pool = pool_from_env()?;
    let schema_ready = match init_db(&pool).await {
        Ok(()) => true,
        Err(error) => {
            eprintln!("database initialization deferred: {error}");
            false
        }
    };

    let state = web::Data::new(AppState {
        pool,
        schema_ready: AtomicBool::new(schema_ready),
        init_lock: Mutex::new(()),
    });
    let port = env::var("PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(5001);
    let workers = thread::available_parallelism()
        .map(|count| count.get())
        .unwrap_or(4);

    HttpServer::new(move || {
        App::new()
            .app_data(state.clone())
            .wrap(Logger::default())
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
    .workers(workers)
    .bind(("0.0.0.0", port))?
    .run()
    .await
}
