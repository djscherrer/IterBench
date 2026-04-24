use std::{env, io};

use actix_web::{delete, get, post, put, web, App, HttpResponse, HttpServer};
use chrono::{DateTime, Utc};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use serde::{Deserialize, Serialize};
use tokio_postgres::{NoTls, Row};

#[derive(Clone)]
struct AppState {
    pool: Pool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Pet {
    id: Option<i64>,
    name: String,
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
struct FindByStatusQuery {
    status: String,
}

#[derive(Debug, Deserialize)]
struct LoginQuery {
    username: String,
    password: String,
}

fn valid_pet_status(status: &str) -> bool {
    matches!(status, "available" | "pending" | "sold")
}

fn valid_order_status(status: &str) -> bool {
    matches!(status, "placed" | "approved" | "delivered")
}

fn validate_pet(pet: &Pet) -> Result<(), HttpResponse> {
    if pet.name.trim().is_empty() || pet.photo_urls.is_empty() {
        return Err(HttpResponse::BadRequest().finish());
    }
    if let Some(status) = &pet.status {
        if !valid_pet_status(status) {
            return Err(HttpResponse::BadRequest().finish());
        }
    }
    Ok(())
}

fn validate_order(order: &Order) -> Result<(), HttpResponse> {
    if let Some(quantity) = order.quantity {
        if quantity < 0 {
            return Err(HttpResponse::BadRequest().finish());
        }
    }
    if let Some(status) = &order.status {
        if !valid_order_status(status) {
            return Err(HttpResponse::BadRequest().finish());
        }
    }
    Ok(())
}

fn normalize_username(username: &str) -> Option<String> {
    let trimmed = username.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_owned())
    }
}

fn validate_user_for_create(user: &User) -> Result<String, HttpResponse> {
    match user.username.as_deref().and_then(normalize_username) {
        Some(username) => Ok(username),
        None => Err(HttpResponse::BadRequest().finish()),
    }
}

fn pet_from_row(row: &Row) -> Pet {
    Pet {
        id: Some(row.get::<_, i64>("id")),
        name: row.get("name"),
        photo_urls: row.get("photo_urls"),
        status: row.get("status"),
    }
}

fn order_from_row(row: &Row) -> Order {
    Order {
        id: Some(row.get::<_, i64>("id")),
        pet_id: row.get("pet_id"),
        quantity: row.get("quantity"),
        ship_date: row.get("ship_date"),
        status: row.get("status"),
        complete: row.get("complete"),
    }
}

fn user_from_row(row: &Row) -> User {
    User {
        id: Some(row.get::<_, i64>("id")),
        username: Some(row.get::<_, String>("username")),
        first_name: row.get("first_name"),
        last_name: row.get("last_name"),
        email: row.get("email"),
        password: row.get("password"),
        phone: row.get("phone"),
        user_status: row.get("user_status"),
    }
}

fn internal_server_error(context: &str, err: &impl std::fmt::Display) -> HttpResponse {
    eprintln!("{context}: {err}");
    HttpResponse::InternalServerError().finish()
}

async fn db_client(state: &web::Data<AppState>) -> Result<deadpool_postgres::Object, HttpResponse> {
    state
        .pool
        .get()
        .await
        .map_err(|err| internal_server_error("database pool error", &err))
}

#[post("/pet")]
async fn add_pet(state: web::Data<AppState>, payload: web::Json<Pet>) -> HttpResponse {
    let pet = payload.into_inner();
    if let Err(response) = validate_pet(&pet) {
        return response;
    }

    let client = match db_client(&state).await {
        Ok(client) => client,
        Err(response) => return response,
    };

    let result = if let Some(id) = pet.id {
        client
            .query_one(
                "INSERT INTO pets (id, name, photo_urls, status) VALUES ($1, $2, $3, $4) RETURNING id, name, photo_urls, status",
                &[&id, &pet.name, &pet.photo_urls, &pet.status],
            )
            .await
    } else {
        client
            .query_one(
                "INSERT INTO pets (name, photo_urls, status) VALUES ($1, $2, $3) RETURNING id, name, photo_urls, status",
                &[&pet.name, &pet.photo_urls, &pet.status],
            )
            .await
    };

    match result {
        Ok(row) => HttpResponse::Ok().json(pet_from_row(&row)),
        Err(err) if err.as_db_error().is_some() => HttpResponse::BadRequest().finish(),
        Err(err) => internal_server_error("failed to add pet", &err),
    }
}

#[put("/pet")]
async fn update_pet(state: web::Data<AppState>, payload: web::Json<Pet>) -> HttpResponse {
    let pet = payload.into_inner();
    if let Err(response) = validate_pet(&pet) {
        return response;
    }

    let Some(id) = pet.id else {
        return HttpResponse::NotFound().finish();
    };

    let client = match db_client(&state).await {
        Ok(client) => client,
        Err(response) => return response,
    };

    match client
        .query_opt(
            "UPDATE pets SET name = $2, photo_urls = $3, status = $4 WHERE id = $1 RETURNING id, name, photo_urls, status",
            &[&id, &pet.name, &pet.photo_urls, &pet.status],
        )
        .await
    {
        Ok(Some(row)) => HttpResponse::Ok().json(pet_from_row(&row)),
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(err) if err.as_db_error().is_some() => HttpResponse::BadRequest().finish(),
        Err(err) => internal_server_error("failed to update pet", &err),
    }
}

#[get("/pet/findByStatus")]
async fn find_pets_by_status(
    state: web::Data<AppState>,
    query: web::Query<FindByStatusQuery>,
) -> HttpResponse {
    if !valid_pet_status(&query.status) {
        return HttpResponse::BadRequest().finish();
    }

    let client = match db_client(&state).await {
        Ok(client) => client,
        Err(response) => return response,
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
        Err(err) => internal_server_error("failed to find pets by status", &err),
    }
}

#[get("/pet/{pet_id}")]
async fn get_pet_by_id(state: web::Data<AppState>, pet_id: web::Path<i64>) -> HttpResponse {
    let client = match db_client(&state).await {
        Ok(client) => client,
        Err(response) => return response,
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
        Err(err) => internal_server_error("failed to get pet", &err),
    }
}

#[delete("/pet/{pet_id}")]
async fn delete_pet(state: web::Data<AppState>, pet_id: web::Path<i64>) -> HttpResponse {
    let client = match db_client(&state).await {
        Ok(client) => client,
        Err(response) => return response,
    };

    match client
        .execute("DELETE FROM pets WHERE id = $1", &[&pet_id.into_inner()])
        .await
    {
        Ok(0) => HttpResponse::NotFound().finish(),
        Ok(_) => HttpResponse::Ok().finish(),
        Err(err) => internal_server_error("failed to delete pet", &err),
    }
}

#[post("/store/order")]
async fn place_order(state: web::Data<AppState>, payload: web::Json<Order>) -> HttpResponse {
    let order = payload.into_inner();
    if let Err(response) = validate_order(&order) {
        return response;
    }

    let client = match db_client(&state).await {
        Ok(client) => client,
        Err(response) => return response,
    };

    let result = if let Some(id) = order.id {
        client
            .query_one(
                "INSERT INTO orders (id, pet_id, quantity, ship_date, status, complete) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id, pet_id, quantity, ship_date, status, complete",
                &[&id, &order.pet_id, &order.quantity, &order.ship_date, &order.status, &order.complete],
            )
            .await
    } else {
        client
            .query_one(
                "INSERT INTO orders (pet_id, quantity, ship_date, status, complete) VALUES ($1, $2, $3, $4, $5) RETURNING id, pet_id, quantity, ship_date, status, complete",
                &[&order.pet_id, &order.quantity, &order.ship_date, &order.status, &order.complete],
            )
            .await
    };

    match result {
        Ok(row) => HttpResponse::Ok().json(order_from_row(&row)),
        Err(err) if err.as_db_error().is_some() => HttpResponse::BadRequest().finish(),
        Err(err) => internal_server_error("failed to place order", &err),
    }
}

#[get("/store/order/{order_id}")]
async fn get_order_by_id(state: web::Data<AppState>, order_id: web::Path<i64>) -> HttpResponse {
    let client = match db_client(&state).await {
        Ok(client) => client,
        Err(response) => return response,
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
        Err(err) => internal_server_error("failed to get order", &err),
    }
}

#[delete("/store/order/{order_id}")]
async fn delete_order(state: web::Data<AppState>, order_id: web::Path<i64>) -> HttpResponse {
    let client = match db_client(&state).await {
        Ok(client) => client,
        Err(response) => return response,
    };

    match client
        .execute("DELETE FROM orders WHERE id = $1", &[&order_id.into_inner()])
        .await
    {
        Ok(0) => HttpResponse::NotFound().finish(),
        Ok(_) => HttpResponse::Ok().finish(),
        Err(err) => internal_server_error("failed to delete order", &err),
    }
}

#[post("/user")]
async fn create_user(state: web::Data<AppState>, payload: web::Json<User>) -> HttpResponse {
    let user = payload.into_inner();
    let username = match validate_user_for_create(&user) {
        Ok(username) => username,
        Err(response) => return response,
    };

    let client = match db_client(&state).await {
        Ok(client) => client,
        Err(response) => return response,
    };

    let result = if let Some(id) = user.id {
        client
            .query_one(
                "INSERT INTO users (id, username, first_name, last_name, email, password, phone, user_status) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id, username, first_name, last_name, email, password, phone, user_status",
                &[&id, &username, &user.first_name, &user.last_name, &user.email, &user.password, &user.phone, &user.user_status],
            )
            .await
    } else {
        client
            .query_one(
                "INSERT INTO users (username, first_name, last_name, email, password, phone, user_status) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id, username, first_name, last_name, email, password, phone, user_status",
                &[&username, &user.first_name, &user.last_name, &user.email, &user.password, &user.phone, &user.user_status],
            )
            .await
    };

    match result {
        Ok(row) => HttpResponse::Ok().json(user_from_row(&row)),
        Err(err) if err.as_db_error().is_some() => HttpResponse::BadRequest().finish(),
        Err(err) => internal_server_error("failed to create user", &err),
    }
}

#[get("/user/login")]
async fn login_user(state: web::Data<AppState>, query: web::Query<LoginQuery>) -> HttpResponse {
    let client = match db_client(&state).await {
        Ok(client) => client,
        Err(response) => return response,
    };

    match client
        .query_opt(
            "SELECT password FROM users WHERE username = $1",
            &[&query.username],
        )
        .await
    {
        Ok(Some(row)) => {
            let stored_password: Option<String> = row.get("password");
            if stored_password.as_deref() == Some(query.password.as_str()) {
                HttpResponse::Ok().json(format!("User {} logged in", query.username))
            } else {
                HttpResponse::BadRequest().finish()
            }
        }
        Ok(None) => HttpResponse::BadRequest().finish(),
        Err(err) => internal_server_error("failed to login user", &err),
    }
}

#[get("/user/{username}")]
async fn get_user_by_name(state: web::Data<AppState>, username: web::Path<String>) -> HttpResponse {
    let client = match db_client(&state).await {
        Ok(client) => client,
        Err(response) => return response,
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
        Err(err) => internal_server_error("failed to get user", &err),
    }
}

#[put("/user/{username}")]
async fn update_user(
    state: web::Data<AppState>,
    username: web::Path<String>,
    payload: web::Json<User>,
) -> HttpResponse {
    let existing_username = username.into_inner();
    let user = payload.into_inner();
    let new_username = user
        .username
        .as_deref()
        .and_then(normalize_username)
        .unwrap_or_else(|| existing_username.clone());

    let client = match db_client(&state).await {
        Ok(client) => client,
        Err(response) => return response,
    };

    match client
        .query_opt(
            "UPDATE users SET username = $2, first_name = $3, last_name = $4, email = $5, password = $6, phone = $7, user_status = $8 WHERE username = $1 RETURNING id, username, first_name, last_name, email, password, phone, user_status",
            &[&existing_username, &new_username, &user.first_name, &user.last_name, &user.email, &user.password, &user.phone, &user.user_status],
        )
        .await
    {
        Ok(Some(row)) => HttpResponse::Ok().json(user_from_row(&row)),
        Ok(None) => HttpResponse::NotFound().finish(),
        Err(err) if err.as_db_error().is_some() => HttpResponse::BadRequest().finish(),
        Err(err) => internal_server_error("failed to update user", &err),
    }
}

#[delete("/user/{username}")]
async fn delete_user(state: web::Data<AppState>, username: web::Path<String>) -> HttpResponse {
    let client = match db_client(&state).await {
        Ok(client) => client,
        Err(response) => return response,
    };

    match client
        .execute("DELETE FROM users WHERE username = $1", &[&username.into_inner()])
        .await
    {
        Ok(0) => HttpResponse::NotFound().finish(),
        Ok(_) => HttpResponse::Ok().finish(),
        Err(err) => internal_server_error("failed to delete user", &err),
    }
}

fn required_env(name: &str) -> io::Result<String> {
    env::var(name).map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, format!("missing environment variable {name}")))
}

fn max_pool_size() -> usize {
    std::thread::available_parallelism()
        .map(|value| value.get() * 8)
        .unwrap_or(32)
        .max(16)
}

fn build_pool_for_host(host: &str, port: u16, user: &str, password: &str, dbname: &str) -> io::Result<Pool> {
    let mut config = tokio_postgres::Config::new();
    config.host(host);
    config.port(port);
    config.user(user);
    config.password(password);
    config.dbname(dbname);

    let manager_config = ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    };
    let manager = Manager::from_config(config, NoTls, manager_config);

    Pool::builder(manager)
        .max_size(max_pool_size())
        .build()
        .map_err(|err| io::Error::new(io::ErrorKind::Other, format!("failed to build pool: {err}")))
}

async fn connect_pool() -> io::Result<Pool> {
    let primary_host = required_env("DB_HOST")?;
    let port = required_env("DB_PORT")?
        .parse::<u16>()
        .map_err(|err| io::Error::new(io::ErrorKind::InvalidInput, format!("invalid DB_PORT: {err}")))?;
    let user = required_env("DB_USER")?;
    let password = required_env("DB_PASSWORD")?;
    let dbname = required_env("DB_NAME")?;

    let mut hosts = vec![primary_host.clone()];
    if primary_host != "localhost" {
        hosts.push("localhost".to_string());
    }
    if primary_host != "127.0.0.1" {
        hosts.push("127.0.0.1".to_string());
    }

    let mut last_error = None;
    for host in hosts {
        let pool = build_pool_for_host(&host, port, &user, &password, &dbname)?;
        match init_db(&pool).await {
            Ok(()) => return Ok(pool),
            Err(err) => last_error = Some(io::Error::new(io::ErrorKind::Other, format!("database initialization failed for host {host}: {err}"))),
        }
    }

    Err(last_error.unwrap_or_else(|| io::Error::new(io::ErrorKind::Other, "database initialization failed")))
}

async fn init_db(pool: &Pool) -> io::Result<()> {
    let client = pool
        .get()
        .await
        .map_err(|err| io::Error::new(io::ErrorKind::Other, format!("failed to get database connection: {err}")))?;

    client
        .batch_execute(
            "
            CREATE TABLE IF NOT EXISTS pets (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                name TEXT NOT NULL,
                photo_urls TEXT[] NOT NULL,
                status TEXT,
                CONSTRAINT pets_status_check CHECK (status IS NULL OR status IN ('available', 'pending', 'sold'))
            );

            CREATE INDEX IF NOT EXISTS idx_pets_status ON pets (status);

            CREATE TABLE IF NOT EXISTS orders (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                pet_id BIGINT,
                quantity INTEGER,
                ship_date TIMESTAMPTZ,
                status TEXT,
                complete BOOLEAN,
                CONSTRAINT orders_status_check CHECK (status IS NULL OR status IN ('placed', 'approved', 'delivered')),
                CONSTRAINT orders_quantity_check CHECK (quantity IS NULL OR quantity >= 0)
            );

            CREATE INDEX IF NOT EXISTS idx_orders_pet_id ON orders (pet_id);

            CREATE TABLE IF NOT EXISTS users (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                first_name TEXT,
                last_name TEXT,
                email TEXT,
                password TEXT,
                phone TEXT,
                user_status INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);
            ",
        )
        .await
        .map_err(|err| io::Error::new(io::ErrorKind::Other, format!("failed to initialize database: {err}")))?;

    Ok(())
}

#[actix_web::main]
async fn main() -> io::Result<()> {
    env_logger::init();

    let pool = connect_pool().await?;

    let port = env::var("PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(5001);
    let workers = std::thread::available_parallelism()
        .map(|value| value.get())
        .unwrap_or(4)
        .max(2);
    let state = web::Data::new(AppState { pool });

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
            .service(login_user)
            .service(create_user)
            .service(get_user_by_name)
            .service(update_user)
            .service(delete_user)
    })
    .workers(workers)
    .bind(("0.0.0.0", port))?
    .run()
    .await
}
