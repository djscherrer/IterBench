use actix_web::{delete, get, post, put, web, App, HttpResponse, HttpServer, Responder};
use chrono::{DateTime, Utc};
use deadpool_postgres::{Config, ManagerConfig, Pool, RecyclingMethod, Runtime};
use serde::{Deserialize, Serialize};
use std::env;

#[derive(Debug, Deserialize, Serialize)]
struct Pet {
    id: Option<i64>,
    name: String,
    photo_urls: Vec<String>,
    status: Option<PetStatus>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "lowercase")]
enum PetStatus {
    Available,
    Pending,
    Sold,
}

#[derive(Debug, Deserialize, Serialize)]
struct Order {
    id: Option<i64>,
    pet_id: Option<i64>,
    quantity: Option<i32>,
    ship_date: Option<DateTime<Utc>>,
    status: Option<OrderStatus>,
    complete: Option<bool>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "lowercase")]
enum OrderStatus {
    Placed,
    Approved,
    Delivered,
}

#[derive(Debug, Deserialize, Serialize)]
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

async fn create_tables(pool: &Pool) -> Result<(), Box<dyn std::error::Error>> {
    let client = pool.get().await?;

    client
        .batch_execute(
            r#"
            CREATE TABLE IF NOT EXISTS pets (
                id BIGSERIAL PRIMARY KEY,
                name VARCHAR NOT NULL,
                photo_urls TEXT[] NOT NULL,
                status VARCHAR CHECK (status IN ('available', 'pending', 'sold'))
            );

            CREATE TABLE IF NOT EXISTS orders (
                id BIGSERIAL PRIMARY KEY,
                pet_id BIGINT,
                quantity INTEGER,
                ship_date TIMESTAMPTZ,
                status VARCHAR CHECK (status IN ('placed', 'approved', 'delivered')),
                complete BOOLEAN
            );

            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                username VARCHAR UNIQUE,
                first_name VARCHAR,
                last_name VARCHAR,
                email VARCHAR,
                password VARCHAR,
                phone VARCHAR,
                user_status INTEGER
            );
            "#,
        )
        .await?;

    Ok(())
}

#[post("/pet")]
async fn add_pet(pool: web::Data<Pool>, pet: web::Json<Pet>) -> impl Responder {
    let client = match pool.get().await {
        Ok(client) => client,
        Err(e) => {
            eprintln!("Failed to get database client: {}", e);
            return HttpResponse::InternalServerError().finish();
        }
    };

    let status = pet.status.as_ref().map(|s| s.to_string());

    let row = match client
        .query_one(
            "INSERT INTO pets (name, photo_urls, status) VALUES ($1, $2, $3) RETURNING id",
            &[&pet.name, &pet.photo_urls, &status],
        )
        .await
    {
        Ok(row) => row,
        Err(e) => {
            eprintln!("Failed to insert pet: {}", e);
            return HttpResponse::BadRequest().body("Invalid input");
        }
    };

    let id: i64 = row.get(0);
    let mut response_pet = pet.into_inner();
    response_pet.id = Some(id);
    HttpResponse::Ok().json(response_pet)
}

#[put("/pet")]
async fn update_pet(pool: web::Data<Pool>, pet: web::Json<Pet>) -> impl Responder {
    let client = match pool.get().await {
        Ok(client) => client,
        Err(e) => {
            eprintln!("Failed to get database client: {}", e);
            return HttpResponse::InternalServerError().finish();
        }
    };

    let id = match pet.id {
        Some(id) => id,
        None => return HttpResponse::NotFound().body("Pet not found"),
    };

    let status = pet.status.as_ref().map(|s| s.to_string());

    let result = client
        .execute(
            "UPDATE pets SET name = $1, photo_urls = $2, status = $3 WHERE id = $4",
            &[&pet.name, &pet.photo_urls, &status, &id],
        )
        .await;

    match result {
        Ok(rows_affected) => {
            if rows_affected == 0 {
                HttpResponse::NotFound().body("Pet not found")
            } else {
                HttpResponse::Ok().json(pet.into_inner())
            }
        }
        Err(e) => {
            eprintln!("Failed to update pet: {}", e);
            HttpResponse::InternalServerError().finish()
        }
    }
}

#[get("/pet/findByStatus")]
async fn find_pets_by_status(
    pool: web::Data<Pool>,
    query: web::Query<StatusQuery>,
) -> impl Responder {
    let client = match pool.get().await {
        Ok(client) => client,
        Err(e) => {
            eprintln!("Failed to get database client: {}", e);
            return HttpResponse::InternalServerError().finish();
        }
    };

    let rows = match client
        .query("SELECT id, name, photo_urls, status FROM pets WHERE status = $1", &[&query.status])
        .await
    {
        Ok(rows) => rows,
        Err(e) => {
            eprintln!("Failed to query pets: {}", e);
            return HttpResponse::InternalServerError().finish();
        }
    };

    let pets: Vec<Pet> = rows
        .iter()
        .map(|row| Pet {
            id: row.get(0),
            name: row.get(1),
            photo_urls: row.get(2),
            status: row.get::<_, Option<String>>(3).map(|s| match s.as_str() {
                "available" => PetStatus::Available,
                "pending" => PetStatus::Pending,
                "sold" => PetStatus::Sold,
                _ => PetStatus::Available,
            }),
        })
        .collect();

    HttpResponse::Ok().json(pets)
}

#[derive(Debug, Deserialize)]
struct StatusQuery {
    status: String,
}

#[get("/pet/{pet_id}")]
async fn get_pet_by_id(pool: web::Data<Pool>, path: web::Path<i64>) -> impl Responder {
    let pet_id = path.into_inner();
    let client = match pool.get().await {
        Ok(client) => client,
        Err(e) => {
            eprintln!("Failed to get database client: {}", e);
            return HttpResponse::InternalServerError().finish();
        }
    };

    match client
        .query_opt(
            "SELECT id, name, photo_urls, status FROM pets WHERE id = $1",
            &[&pet_id],
        )
        .await
    {
        Ok(Some(row)) => {
            let pet = Pet {
                id: row.get(0),
                name: row.get(1),
                photo_urls: row.get(2),
                status: row.get::<_, Option<String>>(3).map(|s| match s.as_str() {
                    "available" => PetStatus::Available,
                    "pending" => PetStatus::Pending,
                    "sold" => PetStatus::Sold,
                    _ => PetStatus::Available,
                }),
            };
            HttpResponse::Ok().json(pet)
        }
        Ok(None) => HttpResponse::NotFound().body("Pet not found"),
        Err(e) => {
            eprintln!("Failed to query pet: {}", e);
            HttpResponse::InternalServerError().finish()
        }
    }
}

#[delete("/pet/{pet_id}")]
async fn delete_pet(pool: web::Data<Pool>, path: web::Path<i64>) -> impl Responder {
    let pet_id = path.into_inner();
    let client = match pool.get().await {
        Ok(client) => client,
        Err(e) => {
            eprintln!("Failed to get database client: {}", e);
            return HttpResponse::InternalServerError().finish();
        }
    };

    match client
        .execute("DELETE FROM pets WHERE id = $1", &[&pet_id])
        .await
    {
        Ok(rows_affected) => {
            if rows_affected == 0 {
                HttpResponse::NotFound().body("Pet not found")
            } else {
                HttpResponse::Ok().finish()
            }
        }
        Err(e) => {
            eprintln!("Failed to delete pet: {}", e);
            HttpResponse::InternalServerError().finish()
        }
    }
}

#[post("/store/order")]
async fn place_order(pool: web::Data<Pool>, order: web::Json<Order>) -> impl Responder {
    let client = match pool.get().await {
        Ok(client) => client,
        Err(e) => {
            eprintln!("Failed to get database client: {}", e);
            return HttpResponse::InternalServerError().finish();
        }
    };

    let status = order.status.as_ref().map(|s| s.to_string());

    let row = match client
        .query_one(
            "INSERT INTO orders (pet_id, quantity, ship_date, status, complete) VALUES ($1, $2, $3, $4, $5) RETURNING id",
            &[&order.pet_id, &order.quantity, &order.ship_date, &status, &order.complete],
        )
        .await
    {
        Ok(row) => row,
        Err(e) => {
            eprintln!("Failed to insert order: {}", e);
            return HttpResponse::InternalServerError().finish();
        }
    };

    let id: i64 = row.get(0);
    let mut response_order = order.into_inner();
    response_order.id = Some(id);
    HttpResponse::Ok().json(response_order)
}

#[get("/store/order/{order_id}")]
async fn get_order_by_id(pool: web::Data<Pool>, path: web::Path<i64>) -> impl Responder {
    let order_id = path.into_inner();
    let client = match pool.get().await {
        Ok(client) => client,
        Err(e) => {
            eprintln!("Failed to get database client: {}", e);
            return HttpResponse::InternalServerError().finish();
        }
    };

    match client
        .query_opt(
            "SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id = $1",
            &[&order_id],
        )
        .await
    {
        Ok(Some(row)) => {
            let order = Order {
                id: row.get(0),
                pet_id: row.get(1),
                quantity: row.get(2),
                ship_date: row.get(3),
                status: row.get::<_, Option<String>>(4).map(|s| match s.as_str() {
                    "placed" => OrderStatus::Placed,
                    "approved" => OrderStatus::Approved,
                    "delivered" => OrderStatus::Delivered,
                    _ => OrderStatus::Placed,
                }),
                complete: row.get(5),
            };
            HttpResponse::Ok().json(order)
        }
        Ok(None) => HttpResponse::NotFound().body("Order not found"),
        Err(e) => {
            eprintln!("Failed to query order: {}", e);
            HttpResponse::InternalServerError().finish()
        }
    }
}

#[delete("/store/order/{order_id}")]
async fn delete_order(pool: web::Data<Pool>, path: web::Path<i64>) -> impl Responder {
    let order_id = path.into_inner();
    let client = match pool.get().await {
        Ok(client) => client,
        Err(e) => {
            eprintln!("Failed to get database client: {}", e);
            return HttpResponse::InternalServerError().finish();
        }
    };

    match client
        .execute("DELETE FROM orders WHERE id = $1", &[&order_id])
        .await
    {
        Ok(rows_affected) => {
            if rows_affected == 0 {
                HttpResponse::NotFound().body("Order not found")
            } else {
                HttpResponse::Ok().finish()
            }
        }
        Err(e) => {
            eprintln!("Failed to delete order: {}", e);
            HttpResponse::InternalServerError().finish()
        }
    }
}

#[post("/user")]
async fn create_user(pool: web::Data<Pool>, user: web::Json<User>) -> impl Responder {
    let client = match pool.get().await {
        Ok(client) => client,
        Err(e) => {
            eprintln!("Failed to get database client: {}", e);
            return HttpResponse::InternalServerError().finish();
        }
    };

    let row = match client
        .query_one(
            "INSERT INTO users (username, first_name, last_name, email, password, phone, user_status) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
            &[&user.username, &user.first_name, &user.last_name, &user.email, &user.password, &user.phone, &user.user_status],
        )
        .await
    {
        Ok(row) => row,
        Err(e) => {
            eprintln!("Failed to insert user: {}", e);
            return HttpResponse::InternalServerError().finish();
        }
    };

    let id: i64 = row.get(0);
    let mut response_user = user.into_inner();
    response_user.id = Some(id);
    HttpResponse::Ok().json(response_user)
}

#[get("/user/{username}")]
async fn get_user_by_name(pool: web::Data<Pool>, path: web::Path<String>) -> impl Responder {
    let username = path.into_inner();
    let client = match pool.get().await {
        Ok(client) => client,
        Err(e) => {
            eprintln!("Failed to get database client: {}", e);
            return HttpResponse::InternalServerError().finish();
        }
    };

    match client
        .query_opt(
            "SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username = $1",
            &[&username],
        )
        .await
    {
        Ok(Some(row)) => {
            let user = User {
                id: row.get(0),
                username: row.get(1),
                first_name: row.get(2),
                last_name: row.get(3),
                email: row.get(4),
                password: row.get(5),
                phone: row.get(6),
                user_status: row.get(7),
            };
            HttpResponse::Ok().json(user)
        }
        Ok(None) => HttpResponse::NotFound().body("User not found"),
        Err(e) => {
            eprintln!("Failed to query user: {}", e);
            HttpResponse::InternalServerError().finish()
        }
    }
}

#[put("/user/{username}")]
async fn update_user(
    pool: web::Data<Pool>,
    path: web::Path<String>,
    user: web::Json<User>,
) -> impl Responder {
    let username = path.into_inner();
    let client = match pool.get().await {
        Ok(client) => client,
        Err(e) => {
            eprintln!("Failed to get database client: {}", e);
            return HttpResponse::InternalServerError().finish();
        }
    };

    let result = client
        .execute(
            "UPDATE users SET first_name = $1, last_name = $2, email = $3, password = $4, phone = $5, user_status = $6 WHERE username = $7",
            &[&user.first_name, &user.last_name, &user.email, &user.password, &user.phone, &user.user_status, &username],
        )
        .await;

    match result {
        Ok(rows_affected) => {
            if rows_affected == 0 {
                HttpResponse::NotFound().body("User not found")
            } else {
                let mut response_user = user.into_inner();
                response_user.username = Some(username);
                HttpResponse::Ok().json(response_user)
            }
        }
        Err(e) => {
            eprintln!("Failed to update user: {}", e);
            HttpResponse::InternalServerError().finish()
        }
    }
}

#[delete("/user/{username}")]
async fn delete_user(pool: web::Data<Pool>, path: web::Path<String>) -> impl Responder {
    let username = path.into_inner();
    let client = match pool.get().await {
        Ok(client) => client,
        Err(e) => {
            eprintln!("Failed to get database client: {}", e);
            return HttpResponse::InternalServerError().finish();
        }
    };

    match client
        .execute("DELETE FROM users WHERE username = $1", &[&username])
        .await
    {
        Ok(rows_affected) => {
            if rows_affected == 0 {
                HttpResponse::NotFound().body("User not found")
            } else {
                HttpResponse::Ok().finish()
            }
        }
        Err(e) => {
            eprintln!("Failed to delete user: {}", e);
            HttpResponse::InternalServerError().finish()
        }
    }
}

#[get("/user/login")]
async fn login_user(query: web::Query<LoginQuery>) -> impl Responder {
    // In a real application, you would validate credentials against the database
    // For this implementation, we'll just return a success message
    HttpResponse::Ok().body("Logged in successfully")
}

#[derive(Debug, Deserialize)]
struct LoginQuery {
    username: String,
    password: String,
}

impl ToString for PetStatus {
    fn to_string(&self) -> String {
        match self {
            PetStatus::Available => "available".to_string(),
            PetStatus::Pending => "pending".to_string(),
            PetStatus::Sold => "sold".to_string(),
        }
    }
}

impl ToString for OrderStatus {
    fn to_string(&self) -> String {
        match self {
            OrderStatus::Placed => "placed".to_string(),
            OrderStatus::Approved => "approved".to_string(),
            OrderStatus::Delivered => "delivered".to_string(),
        }
    }
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let db_host = env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string());
    let db_port = env::var("DB_PORT")
        .unwrap_or_else(|_| "5432".to_string())
        .parse::<u16>()
        .unwrap_or(5432);
    let db_user = env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string());
    let db_password = env::var("DB_PASSWORD").unwrap_or_else(|_| "password".to_string());
    let db_name = env::var("DB_NAME").unwrap_or_else(|_| "petstore".to_string());

    let mut cfg = Config::new();
    cfg.host = Some(db_host);
    cfg.port = Some(db_port);
    cfg.user = Some(db_user);
    cfg.password = Some(db_password);
    cfg.dbname = Some(db_name);
    cfg.manager = Some(ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    });

    let pool = cfg.create_pool(Some(Runtime::Tokio1), tokio_postgres::NoTls).unwrap();

    // Create tables on startup
    if let Err(e) = create_tables(&pool).await {
        eprintln!("Failed to create tables: {}", e);
        std::process::exit(1);
    }

    let port = env::var("PORT")
        .unwrap_or_else(|_| "5001".to_string())
        .parse::<u16>()
        .unwrap_or(5001);

    println!("Starting server on 0.0.0.0:{}", port);

    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(pool.clone()))
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