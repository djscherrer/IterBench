use actix_web::{error, web, App, Error, HttpResponse, HttpServer, Result};
use chrono::{DateTime, Utc};
use deadpool_postgres::{
    Config as PgPoolConfig, ManagerConfig, Pool, PoolConfig, RecyclingMethod, Runtime,
};
use serde::{Deserialize, Serialize};
use std::{env, io};
use tokio_postgres::{NoTls, Row};

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Pet {
    id: Option<i64>,
    name: String,
    #[serde(rename = "photoUrls")]
    photo_urls: Vec<String>,
    status: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Order {
    id: Option<i64>,
    pet_id: Option<i64>,
    quantity: Option<i32>,
    ship_date: Option<DateTime<Utc>>,
    status: Option<String>,
    complete: Option<bool>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
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

#[derive(Debug, Deserialize)]
struct StatusQuery {
    status: String,
}

#[derive(Debug, Deserialize)]
struct LoginQuery {
    username: String,
    password: String,
}

fn internal_error<E: std::fmt::Display>(err: E) -> Error {
    error::ErrorInternalServerError(err.to_string())
}

fn validate_status(value: &Option<String>, allowed: &[&str]) -> Result<()> {
    if let Some(status) = value {
        if !allowed
            .iter()
            .any(|allowed_status| *allowed_status == status)
        {
            return Err(error::ErrorBadRequest("invalid status"));
        }
    }
    Ok(())
}

fn validate_query_status(status: &str, allowed: &[&str]) -> Result<()> {
    if allowed
        .iter()
        .any(|allowed_status| *allowed_status == status)
    {
        Ok(())
    } else {
        Err(error::ErrorBadRequest("invalid status"))
    }
}

fn row_to_pet(row: &Row) -> Pet {
    Pet {
        id: Some(row.get::<_, i64>("id")),
        name: row.get("name"),
        photo_urls: row.get("photo_urls"),
        status: row.get("status"),
    }
}

fn row_to_order(row: &Row) -> Order {
    Order {
        id: Some(row.get::<_, i64>("id")),
        pet_id: row.get("pet_id"),
        quantity: row.get("quantity"),
        ship_date: row.get("ship_date"),
        status: row.get("status"),
        complete: row.get("complete"),
    }
}

fn row_to_user(row: &Row) -> User {
    User {
        id: Some(row.get::<_, i64>("id")),
        username: row.get("username"),
        first_name: row.get("first_name"),
        last_name: row.get("last_name"),
        email: row.get("email"),
        password: row.get("password"),
        phone: row.get("phone"),
        user_status: row.get("user_status"),
    }
}

fn create_pool() -> io::Result<Pool> {
    let mut cfg = PgPoolConfig::new();
    cfg.host = Some(env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string()));
    cfg.port = Some(
        env::var("DB_PORT")
            .ok()
            .and_then(|port| port.parse::<u16>().ok())
            .unwrap_or(5432),
    );
    cfg.user = Some(env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string()));
    cfg.password = env::var("DB_PASSWORD").ok();
    cfg.dbname = Some(env::var("DB_NAME").unwrap_or_else(|_| "postgres".to_string()));
    cfg.manager = Some(ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    });
    cfg.pool = Some(PoolConfig::new(64));
    cfg.create_pool(Some(Runtime::Tokio1), NoTls)
        .map_err(|err| io::Error::new(io::ErrorKind::Other, err.to_string()))
}

async fn init_db(pool: &Pool) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let client = pool.get().await?;
    client
        .batch_execute(
            r#"
            CREATE TABLE IF NOT EXISTS pets (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                photo_urls TEXT[] NOT NULL,
                status TEXT CHECK (status IN ('available', 'pending', 'sold'))
            );
            CREATE INDEX IF NOT EXISTS idx_pets_status ON pets(status);

            CREATE TABLE IF NOT EXISTS pet_orders (
                id BIGSERIAL PRIMARY KEY,
                pet_id BIGINT,
                quantity INTEGER,
                ship_date TIMESTAMPTZ,
                status TEXT CHECK (status IN ('placed', 'approved', 'delivered')),
                complete BOOLEAN
            );
            CREATE INDEX IF NOT EXISTS idx_pet_orders_pet_id ON pet_orders(pet_id);
            CREATE INDEX IF NOT EXISTS idx_pet_orders_status ON pet_orders(status);

            CREATE TABLE IF NOT EXISTS app_users (
                id BIGSERIAL PRIMARY KEY,
                username TEXT UNIQUE,
                first_name TEXT,
                last_name TEXT,
                email TEXT,
                password TEXT,
                phone TEXT,
                user_status INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_app_users_username ON app_users(username);
            "#,
        )
        .await?;
    Ok(())
}

async fn add_pet(pool: web::Data<Pool>, body: web::Json<Pet>) -> Result<HttpResponse> {
    let pet = body.into_inner();
    validate_status(&pet.status, &["available", "pending", "sold"])?;

    let client = pool.get().await.map_err(internal_error)?;
    let row = client
        .query_one(
            "INSERT INTO pets (id, name, photo_urls, status)
             VALUES (COALESCE($1::BIGINT, nextval('pets_id_seq')), $2, $3, $4)
             RETURNING id, name, photo_urls, status",
            &[&pet.id, &pet.name, &pet.photo_urls, &pet.status],
        )
        .await
        .map_err(internal_error)?;

    Ok(HttpResponse::Ok().json(row_to_pet(&row)))
}

async fn update_pet(pool: web::Data<Pool>, body: web::Json<Pet>) -> Result<HttpResponse> {
    let pet = body.into_inner();
    validate_status(&pet.status, &["available", "pending", "sold"])?;
    let Some(id) = pet.id else {
        return Ok(HttpResponse::NotFound().finish());
    };

    let client = pool.get().await.map_err(internal_error)?;
    let row = client
        .query_opt(
            "UPDATE pets
             SET name = $2, photo_urls = $3, status = $4
             WHERE id = $1
             RETURNING id, name, photo_urls, status",
            &[&id, &pet.name, &pet.photo_urls, &pet.status],
        )
        .await
        .map_err(internal_error)?;

    match row {
        Some(row) => Ok(HttpResponse::Ok().json(row_to_pet(&row))),
        None => Ok(HttpResponse::NotFound().finish()),
    }
}

async fn find_pets_by_status(
    pool: web::Data<Pool>,
    query: web::Query<StatusQuery>,
) -> Result<HttpResponse> {
    validate_query_status(&query.status, &["available", "pending", "sold"])?;

    let client = pool.get().await.map_err(internal_error)?;
    let rows = client
        .query(
            "SELECT id, name, photo_urls, status FROM pets WHERE status = $1 ORDER BY id",
            &[&query.status],
        )
        .await
        .map_err(internal_error)?;
    let pets: Vec<Pet> = rows.iter().map(row_to_pet).collect();

    Ok(HttpResponse::Ok().json(pets))
}

async fn get_pet_by_id(pool: web::Data<Pool>, path: web::Path<i64>) -> Result<HttpResponse> {
    let pet_id = path.into_inner();
    let client = pool.get().await.map_err(internal_error)?;
    let row = client
        .query_opt(
            "SELECT id, name, photo_urls, status FROM pets WHERE id = $1",
            &[&pet_id],
        )
        .await
        .map_err(internal_error)?;

    match row {
        Some(row) => Ok(HttpResponse::Ok().json(row_to_pet(&row))),
        None => Ok(HttpResponse::NotFound().finish()),
    }
}

async fn delete_pet(pool: web::Data<Pool>, path: web::Path<i64>) -> Result<HttpResponse> {
    let pet_id = path.into_inner();
    let client = pool.get().await.map_err(internal_error)?;
    let deleted = client
        .execute("DELETE FROM pets WHERE id = $1", &[&pet_id])
        .await
        .map_err(internal_error)?;

    if deleted == 0 {
        Ok(HttpResponse::NotFound().finish())
    } else {
        Ok(HttpResponse::Ok().finish())
    }
}

async fn place_order(pool: web::Data<Pool>, body: web::Json<Order>) -> Result<HttpResponse> {
    let order = body.into_inner();
    validate_status(&order.status, &["placed", "approved", "delivered"])?;

    let client = pool.get().await.map_err(internal_error)?;
    let row = client
        .query_one(
            "INSERT INTO pet_orders (id, pet_id, quantity, ship_date, status, complete)
             VALUES (COALESCE($1::BIGINT, nextval('pet_orders_id_seq')), $2, $3, $4, $5, $6)
             RETURNING id, pet_id, quantity, ship_date, status, complete",
            &[
                &order.id,
                &order.pet_id,
                &order.quantity,
                &order.ship_date,
                &order.status,
                &order.complete,
            ],
        )
        .await
        .map_err(internal_error)?;

    Ok(HttpResponse::Ok().json(row_to_order(&row)))
}

async fn get_order_by_id(pool: web::Data<Pool>, path: web::Path<i64>) -> Result<HttpResponse> {
    let order_id = path.into_inner();
    let client = pool.get().await.map_err(internal_error)?;
    let row = client
        .query_opt(
            "SELECT id, pet_id, quantity, ship_date, status, complete FROM pet_orders WHERE id = $1",
            &[&order_id],
        )
        .await
        .map_err(internal_error)?;

    match row {
        Some(row) => Ok(HttpResponse::Ok().json(row_to_order(&row))),
        None => Ok(HttpResponse::NotFound().finish()),
    }
}

async fn delete_order(pool: web::Data<Pool>, path: web::Path<i64>) -> Result<HttpResponse> {
    let order_id = path.into_inner();
    let client = pool.get().await.map_err(internal_error)?;
    let deleted = client
        .execute("DELETE FROM pet_orders WHERE id = $1", &[&order_id])
        .await
        .map_err(internal_error)?;

    if deleted == 0 {
        Ok(HttpResponse::NotFound().finish())
    } else {
        Ok(HttpResponse::Ok().finish())
    }
}

async fn create_user(pool: web::Data<Pool>, body: web::Json<User>) -> Result<HttpResponse> {
    let user = body.into_inner();
    let client = pool.get().await.map_err(internal_error)?;
    let row = client
        .query_one(
            "INSERT INTO app_users
                (id, username, first_name, last_name, email, password, phone, user_status)
             VALUES (COALESCE($1::BIGINT, nextval('app_users_id_seq')), $2, $3, $4, $5, $6, $7, $8)
             RETURNING id, username, first_name, last_name, email, password, phone, user_status",
            &[
                &user.id,
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
        .map_err(internal_error)?;

    Ok(HttpResponse::Ok().json(row_to_user(&row)))
}

async fn get_user_by_name(pool: web::Data<Pool>, path: web::Path<String>) -> Result<HttpResponse> {
    let username = path.into_inner();
    let client = pool.get().await.map_err(internal_error)?;
    let row = client
        .query_opt(
            "SELECT id, username, first_name, last_name, email, password, phone, user_status
             FROM app_users WHERE username = $1",
            &[&username],
        )
        .await
        .map_err(internal_error)?;

    match row {
        Some(row) => Ok(HttpResponse::Ok().json(row_to_user(&row))),
        None => Ok(HttpResponse::NotFound().finish()),
    }
}

async fn update_user(
    pool: web::Data<Pool>,
    path: web::Path<String>,
    body: web::Json<User>,
) -> Result<HttpResponse> {
    let username = path.into_inner();
    let user = body.into_inner();
    let client = pool.get().await.map_err(internal_error)?;
    let row = client
        .query_opt(
            "UPDATE app_users
             SET id = COALESCE($2::BIGINT, id),
                 username = COALESCE($3, username),
                 first_name = COALESCE($4, first_name),
                 last_name = COALESCE($5, last_name),
                 email = COALESCE($6, email),
                 password = COALESCE($7, password),
                 phone = COALESCE($8, phone),
                 user_status = COALESCE($9, user_status)
             WHERE username = $1
             RETURNING id, username, first_name, last_name, email, password, phone, user_status",
            &[
                &username,
                &user.id,
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
        .map_err(internal_error)?;

    match row {
        Some(row) => Ok(HttpResponse::Ok().json(row_to_user(&row))),
        None => Ok(HttpResponse::NotFound().finish()),
    }
}

async fn delete_user(pool: web::Data<Pool>, path: web::Path<String>) -> Result<HttpResponse> {
    let username = path.into_inner();
    let client = pool.get().await.map_err(internal_error)?;
    let deleted = client
        .execute("DELETE FROM app_users WHERE username = $1", &[&username])
        .await
        .map_err(internal_error)?;

    if deleted == 0 {
        Ok(HttpResponse::NotFound().finish())
    } else {
        Ok(HttpResponse::Ok().finish())
    }
}

async fn login_user(pool: web::Data<Pool>, query: web::Query<LoginQuery>) -> Result<HttpResponse> {
    let client = pool.get().await.map_err(internal_error)?;
    let row = client
        .query_opt(
            "SELECT 1 FROM app_users WHERE username = $1 AND password = $2 LIMIT 1",
            &[&query.username, &query.password],
        )
        .await
        .map_err(internal_error)?;

    if row.is_some() {
        Ok(HttpResponse::Ok().json(format!("logged in user session: {}", uuid::Uuid::new_v4())))
    } else {
        Ok(HttpResponse::BadRequest().finish())
    }
}

fn configure_routes(cfg: &mut web::ServiceConfig) {
    cfg.route("/pet", web::post().to(add_pet))
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
        .route("/user/{username}", web::delete().to(delete_user));
}

#[actix_web::main]
async fn main() -> io::Result<()> {
    let _ = env_logger::try_init();
    let pool = create_pool()?;
    init_db(&pool)
        .await
        .map_err(|err| io::Error::new(io::ErrorKind::Other, err.to_string()))?;

    let port = env::var("PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(5001);
    let bind_addr = format!("0.0.0.0:{port}");

    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(pool.clone()))
            .configure(configure_routes)
    })
    .bind(bind_addr)?
    .run()
    .await
}
