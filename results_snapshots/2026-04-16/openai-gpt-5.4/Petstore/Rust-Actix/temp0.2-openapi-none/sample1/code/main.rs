use actix_web::{
    delete, get, post, put,
    web::{self, Data, Json, Path, Query},
    App, HttpResponse, HttpServer, Responder,
};
use chrono::{DateTime, Utc};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use serde::{Deserialize, Serialize};
use std::{env, io};
use tokio_postgres::{types::ToSql, Config, NoTls, Row};

#[derive(Clone)]
struct AppState {
    pool: Pool,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
struct Pet {
    id: Option<i64>,
    name: String,
    photo_urls: Vec<String>,
    status: Option<PetStatus>,
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
#[serde(rename_all = "camelCase")]
struct Order {
    id: Option<i64>,
    pet_id: Option<i64>,
    quantity: Option<i32>,
    ship_date: Option<DateTime<Utc>>,
    status: Option<OrderStatus>,
    complete: Option<bool>,
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

fn pet_from_row(row: &Row) -> Pet {
    let photo_urls: Vec<String> = row.get("photo_urls");
    let status: Option<String> = row.get("status");
    Pet {
        id: Some(row.get("id")),
        name: row.get("name"),
        photo_urls,
        status: status.and_then(|s| PetStatus::from_str(&s)),
    }
}

fn order_from_row(row: &Row) -> Order {
    let status: Option<String> = row.get("status");
    Order {
        id: Some(row.get("id")),
        pet_id: row.get("pet_id"),
        quantity: row.get("quantity"),
        ship_date: row.get("ship_date"),
        status: status.and_then(|s| OrderStatus::from_str(&s)),
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
            r#"
            CREATE TABLE IF NOT EXISTS pets (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                photo_urls TEXT[] NOT NULL,
                status TEXT NULL CHECK (status IN ('available', 'pending', 'sold'))
            );

            CREATE TABLE IF NOT EXISTS orders (
                id BIGSERIAL PRIMARY KEY,
                pet_id BIGINT NULL,
                quantity INTEGER NULL,
                ship_date TIMESTAMPTZ NULL,
                status TEXT NULL CHECK (status IN ('placed', 'approved', 'delivered')),
                complete BOOLEAN NULL,
                CONSTRAINT fk_orders_pet
                    FOREIGN KEY (pet_id)
                    REFERENCES pets(id)
                    ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                username TEXT UNIQUE,
                first_name TEXT NULL,
                last_name TEXT NULL,
                email TEXT NULL,
                password TEXT NULL,
                phone TEXT NULL,
                user_status INTEGER NULL
            );
            "#,
        )
        .await?;

    Ok(())
}

#[post("/pet")]
async fn add_pet(state: Data<AppState>, body: Json<Pet>) -> impl Responder {
    let pet = body.into_inner();

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let status_str = pet.status.as_ref().map(|s| s.as_str());

    let row = if let Some(id) = pet.id {
        match client
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
                &[&id, &pet.name, &pet.photo_urls, &status_str],
            )
            .await
        {
            Ok(row) => row,
            Err(_) => return HttpResponse::BadRequest().finish(),
        }
    } else {
        match client
            .query_one(
                r#"
                INSERT INTO pets (name, photo_urls, status)
                VALUES ($1, $2, $3)
                RETURNING id, name, photo_urls, status
                "#,
                &[&pet.name, &pet.photo_urls, &status_str],
            )
            .await
        {
            Ok(row) => row,
            Err(_) => return HttpResponse::BadRequest().finish(),
        }
    };

    HttpResponse::Ok().json(pet_from_row(&row))
}

#[put("/pet")]
async fn update_pet(state: Data<AppState>, body: Json<Pet>) -> impl Responder {
    let pet = body.into_inner();
    let id = match pet.id {
        Some(id) => id,
        None => return HttpResponse::NotFound().finish(),
    };

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let status_str = pet.status.as_ref().map(|s| s.as_str());

    match client
        .query_opt(
            r#"
            UPDATE pets
            SET name = $2, photo_urls = $3, status = $4
            WHERE id = $1
            RETURNING id, name, photo_urls, status
            "#,
            &[&id, &pet.name, &pet.photo_urls, &status_str],
        )
        .await
    {
        Ok(Some(row)) => HttpResponse::Ok().json(pet_from_row(&row)),
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

#[get("/pet/findByStatus")]
async fn find_pets_by_status(state: Data<AppState>, query: Query<StatusQuery>) -> impl Responder {
    let status = match PetStatus::from_str(&query.status) {
        Some(s) => s,
        None => return HttpResponse::BadRequest().finish(),
    };

    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query(
            "SELECT id, name, photo_urls, status FROM pets WHERE status = $1 ORDER BY id",
            &[&status.as_str()],
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

#[get("/pet/{pet_id}")]
async fn get_pet_by_id(state: Data<AppState>, pet_id: Path<i64>) -> impl Responder {
    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query_opt(
            "SELECT id, name, photo_urls, status FROM pets WHERE id = $1",
            &[&pet_id.into_inner()],
        )
        .await
    {
        Ok(Some(row)) => HttpResponse::Ok().json(pet_from_row(&row)),
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

#[delete("/pet/{pet_id}")]
async fn delete_pet(state: Data<AppState>, pet_id: Path<i64>) -> impl Responder {
    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .execute("DELETE FROM pets WHERE id = $1", &[&pet_id.into_inner()])
        .await
    {
        Ok(0) => HttpResponse::NotFound().finish(),
        Ok(_) => HttpResponse::Ok().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

#[post("/store/order")]
async fn place_order(state: Data<AppState>, body: Json<Order>) -> impl Responder {
    let order = body.into_inner();
    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let status_str = order.status.as_ref().map(|s| s.as_str());

    let row = if let Some(id) = order.id {
        match client
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
                    &id as &(dyn ToSql + Sync),
                    &order.pet_id,
                    &order.quantity,
                    &order.ship_date,
                    &status_str,
                    &order.complete,
                ],
            )
            .await
        {
            Ok(row) => row,
            Err(_) => return HttpResponse::BadRequest().finish(),
        }
    } else {
        match client
            .query_one(
                r#"
                INSERT INTO orders (pet_id, quantity, ship_date, status, complete)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, pet_id, quantity, ship_date, status, complete
                "#,
                &[
                    &order.pet_id as &(dyn ToSql + Sync),
                    &order.quantity,
                    &order.ship_date,
                    &status_str,
                    &order.complete,
                ],
            )
            .await
        {
            Ok(row) => row,
            Err(_) => return HttpResponse::BadRequest().finish(),
        }
    };

    HttpResponse::Ok().json(order_from_row(&row))
}

#[get("/store/order/{order_id}")]
async fn get_order_by_id(state: Data<AppState>, order_id: Path<i64>) -> impl Responder {
    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query_opt(
            "SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id = $1",
            &[&order_id.into_inner()],
        )
        .await
    {
        Ok(Some(row)) => HttpResponse::Ok().json(order_from_row(&row)),
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

#[delete("/store/order/{order_id}")]
async fn delete_order(state: Data<AppState>, order_id: Path<i64>) -> impl Responder {
    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .execute("DELETE FROM orders WHERE id = $1", &[&order_id.into_inner()])
        .await
    {
        Ok(0) => HttpResponse::NotFound().finish(),
        Ok(_) => HttpResponse::Ok().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

#[post("/user")]
async fn create_user(state: Data<AppState>, body: Json<User>) -> impl Responder {
    let user = body.into_inner();
    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let row = if let Some(id) = user.id {
        match client
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
                    &id as &(dyn ToSql + Sync),
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
            Err(_) => return HttpResponse::BadRequest().finish(),
        }
    } else {
        match client
            .query_one(
                r#"
                INSERT INTO users (username, first_name, last_name, email, password, phone, user_status)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id, username, first_name, last_name, email, password, phone, user_status
                "#,
                &[
                    &user.username as &(dyn ToSql + Sync),
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
            Err(_) => return HttpResponse::BadRequest().finish(),
        }
    };

    HttpResponse::Ok().json(user_from_row(&row))
}

#[get("/user/{username}")]
async fn get_user_by_name(state: Data<AppState>, username: Path<String>) -> impl Responder {
    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .query_opt(
            "SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username = $1",
            &[&username.into_inner()],
        )
        .await
    {
        Ok(Some(row)) => HttpResponse::Ok().json(user_from_row(&row)),
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

#[put("/user/{username}")]
async fn update_user(
    state: Data<AppState>,
    username: Path<String>,
    body: Json<User>,
) -> impl Responder {
    let path_username = username.into_inner();
    let user = body.into_inner();

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
                &path_username as &(dyn ToSql + Sync),
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

#[delete("/user/{username}")]
async fn delete_user(state: Data<AppState>, username: Path<String>) -> impl Responder {
    let client = match state.pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    match client
        .execute("DELETE FROM users WHERE username = $1", &[&username.into_inner()])
        .await
    {
        Ok(0) => HttpResponse::NotFound().finish(),
        Ok(_) => HttpResponse::Ok().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

#[get("/user/login")]
async fn login_user(state: Data<AppState>, query: Query<LoginQuery>) -> impl Responder {
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
        Ok(Some(_)) => HttpResponse::Ok().json(format!("User {} logged in successfully", query.username)),
        Ok(None) => HttpResponse::BadRequest().finish(),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

fn build_db_config_from_env() -> Result<Config, io::Error> {
    let host = env::var("DB_HOST")
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "DB_HOST is not set"))?;
    let port = env::var("DB_PORT")
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "DB_PORT is not set"))?
        .parse::<u16>()
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "DB_PORT is invalid"))?;
    let user = env::var("DB_USER")
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "DB_USER is not set"))?;
    let password = env::var("DB_PASSWORD")
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "DB_PASSWORD is not set"))?;
    let dbname = env::var("DB_NAME")
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "DB_NAME is not set"))?;

    let mut cfg = Config::new();
    cfg.host(&host);
    cfg.port(port);
    cfg.user(&user);
    cfg.password(&password);
    cfg.dbname(&dbname);
    Ok(cfg)
}

fn build_pool(pg_config: Config) -> Result<Pool, io::Error> {
    let mgr_config = ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    };
    let manager = Manager::from_config(pg_config, NoTls, mgr_config);
    Pool::builder(manager)
        .max_size(16)
        .build()
        .map_err(|e| io::Error::new(io::ErrorKind::Other, format!("failed to build pool: {e}")))
}

#[actix_web::main]
async fn main() -> io::Result<()> {
    env_logger::init();

    let pg_config = build_db_config_from_env()?;
    let pool = build_pool(pg_config)?;

    init_db(&pool)
        .await
        .map_err(|e| io::Error::new(io::ErrorKind::Other, format!("database init failed: {e}")))?;

    let state = Data::new(AppState { pool });

    let port = env::var("PORT")
        .ok()
        .and_then(|p| p.parse::<u16>().ok())
        .unwrap_or(5001);

    HttpServer::new(move || {
        App::new()
            .app_data(state.clone())
            .service(add_pet)
            .service(update_pet)
            .service(find_pets_by_status)
            .service(get_pet_by_id)
            .service(delete_pet)
            .service(place_order)
            .service(get_order_by_id)
            .service(delete_order)
            .service(create_user)
            .service(get_user_by_name)
            .service(update_user)
            .service(delete_user)
            .service(login_user)
    })
    .bind(("0.0.0.0", port))?
    .run()
    .await
}