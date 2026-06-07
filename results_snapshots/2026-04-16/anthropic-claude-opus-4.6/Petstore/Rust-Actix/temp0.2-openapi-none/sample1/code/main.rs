use actix_web::{web, App, HttpServer, HttpResponse, middleware};
use deadpool_postgres::{Config, Pool, Runtime};
use tokio_postgres::NoTls;
use serde::{Deserialize, Serialize};
use chrono::{DateTime, Utc};
use std::env;

// ─── Models ───

#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
struct Pet {
    #[serde(skip_serializing_if = "Option::is_none")]
    id: Option<i64>,
    name: String,
    photo_urls: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    status: Option<String>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
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
    status: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    complete: Option<bool>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
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
struct FindByStatusQuery {
    status: String,
}

#[derive(Debug, Deserialize)]
struct LoginQuery {
    username: String,
    password: String,
}

// ─── Database initialization ───

async fn init_db(pool: &Pool) {
    let client = pool.get().await.expect("Failed to get DB client for init");

    client
        .batch_execute(
            "
            CREATE TABLE IF NOT EXISTS pets (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                photo_urls TEXT[] NOT NULL DEFAULT '{}',
                status TEXT
            );

            CREATE TABLE IF NOT EXISTS orders (
                id BIGSERIAL PRIMARY KEY,
                pet_id BIGINT,
                quantity INT,
                ship_date TIMESTAMPTZ,
                status TEXT,
                complete BOOLEAN DEFAULT FALSE
            );

            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                username TEXT UNIQUE,
                first_name TEXT,
                last_name TEXT,
                email TEXT,
                password TEXT,
                phone TEXT,
                user_status INT
            );
            ",
        )
        .await
        .expect("Failed to initialize database tables");
}

// ─── Pet handlers ───

async fn add_pet(pool: web::Data<Pool>, body: web::Json<Pet>) -> HttpResponse {
    let pet = body.into_inner();

    // Validate status if provided
    if let Some(ref s) = pet.status {
        if !["available", "pending", "sold"].contains(&s.as_str()) {
            return HttpResponse::BadRequest().json(serde_json::json!({"message": "Invalid input"}));
        }
    }

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let row = client
        .query_one(
            "INSERT INTO pets (name, photo_urls, status) VALUES ($1, $2, $3) RETURNING id, name, photo_urls, status",
            &[&pet.name, &pet.photo_urls, &pet.status],
        )
        .await;

    match row {
        Ok(row) => {
            let result = Pet {
                id: Some(row.get("id")),
                name: row.get("name"),
                photo_urls: row.get("photo_urls"),
                status: row.get("status"),
            };
            HttpResponse::Ok().json(result)
        }
        Err(_) => HttpResponse::BadRequest().json(serde_json::json!({"message": "Invalid input"})),
    }
}

async fn update_pet(pool: web::Data<Pool>, body: web::Json<Pet>) -> HttpResponse {
    let pet = body.into_inner();

    let pet_id = match pet.id {
        Some(id) => id,
        None => return HttpResponse::NotFound().json(serde_json::json!({"message": "Pet not found"})),
    };

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let result = client
        .query_opt(
            "UPDATE pets SET name = $1, photo_urls = $2, status = $3 WHERE id = $4 RETURNING id, name, photo_urls, status",
            &[&pet.name, &pet.photo_urls, &pet.status, &pet_id],
        )
        .await;

    match result {
        Ok(Some(row)) => {
            let result = Pet {
                id: Some(row.get("id")),
                name: row.get("name"),
                photo_urls: row.get("photo_urls"),
                status: row.get("status"),
            };
            HttpResponse::Ok().json(result)
        }
        Ok(None) => HttpResponse::NotFound().json(serde_json::json!({"message": "Pet not found"})),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn find_pets_by_status(pool: web::Data<Pool>, query: web::Query<FindByStatusQuery>) -> HttpResponse {
    let status = &query.status;

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let rows = client
        .query(
            "SELECT id, name, photo_urls, status FROM pets WHERE status = $1",
            &[status],
        )
        .await;

    match rows {
        Ok(rows) => {
            let pets: Vec<Pet> = rows
                .iter()
                .map(|row| Pet {
                    id: Some(row.get("id")),
                    name: row.get("name"),
                    photo_urls: row.get("photo_urls"),
                    status: row.get("status"),
                })
                .collect();
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

    let result = client
        .query_opt(
            "SELECT id, name, photo_urls, status FROM pets WHERE id = $1",
            &[&pet_id],
        )
        .await;

    match result {
        Ok(Some(row)) => {
            let pet = Pet {
                id: Some(row.get("id")),
                name: row.get("name"),
                photo_urls: row.get("photo_urls"),
                status: row.get("status"),
            };
            HttpResponse::Ok().json(pet)
        }
        Ok(None) => HttpResponse::NotFound().json(serde_json::json!({"message": "Pet not found"})),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn delete_pet(pool: web::Data<Pool>, path: web::Path<i64>) -> HttpResponse {
    let pet_id = path.into_inner();

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let result = client
        .execute("DELETE FROM pets WHERE id = $1", &[&pet_id])
        .await;

    match result {
        Ok(count) if count > 0 => HttpResponse::Ok().json(serde_json::json!({"message": "successful operation"})),
        Ok(_) => HttpResponse::NotFound().json(serde_json::json!({"message": "Pet not found"})),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

// ─── Store/Order handlers ───

async fn place_order(pool: web::Data<Pool>, body: web::Json<Order>) -> HttpResponse {
    let order = body.into_inner();

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let row = client
        .query_one(
            "INSERT INTO orders (pet_id, quantity, ship_date, status, complete) VALUES ($1, $2, $3, $4, $5) RETURNING id, pet_id, quantity, ship_date, status, complete",
            &[&order.pet_id, &order.quantity, &order.ship_date, &order.status, &order.complete],
        )
        .await;

    match row {
        Ok(row) => {
            let result = Order {
                id: Some(row.get("id")),
                pet_id: row.get("pet_id"),
                quantity: row.get("quantity"),
                ship_date: row.get("ship_date"),
                status: row.get("status"),
                complete: row.get("complete"),
            };
            HttpResponse::Ok().json(result)
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

    let result = client
        .query_opt(
            "SELECT id, pet_id, quantity, ship_date, status, complete FROM orders WHERE id = $1",
            &[&order_id],
        )
        .await;

    match result {
        Ok(Some(row)) => {
            let order = Order {
                id: Some(row.get("id")),
                pet_id: row.get("pet_id"),
                quantity: row.get("quantity"),
                ship_date: row.get("ship_date"),
                status: row.get("status"),
                complete: row.get("complete"),
            };
            HttpResponse::Ok().json(order)
        }
        Ok(None) => HttpResponse::NotFound().json(serde_json::json!({"message": "Order not found"})),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn delete_order(pool: web::Data<Pool>, path: web::Path<i64>) -> HttpResponse {
    let order_id = path.into_inner();

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let result = client
        .execute("DELETE FROM orders WHERE id = $1", &[&order_id])
        .await;

    match result {
        Ok(count) if count > 0 => HttpResponse::Ok().json(serde_json::json!({"message": "successful operation"})),
        Ok(_) => HttpResponse::NotFound().json(serde_json::json!({"message": "Order not found"})),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

// ─── User handlers ───

async fn create_user(pool: web::Data<Pool>, body: web::Json<User>) -> HttpResponse {
    let user = body.into_inner();

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let row = client
        .query_one(
            "INSERT INTO users (username, first_name, last_name, email, password, phone, user_status) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id, username, first_name, last_name, email, password, phone, user_status",
            &[&user.username, &user.first_name, &user.last_name, &user.email, &user.password, &user.phone, &user.user_status],
        )
        .await;

    match row {
        Ok(row) => {
            let result = User {
                id: Some(row.get("id")),
                username: row.get("username"),
                first_name: row.get("first_name"),
                last_name: row.get("last_name"),
                email: row.get("email"),
                password: row.get("password"),
                phone: row.get("phone"),
                user_status: row.get("user_status"),
            };
            HttpResponse::Ok().json(result)
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

    let result = client
        .query_opt(
            "SELECT id, username, first_name, last_name, email, password, phone, user_status FROM users WHERE username = $1",
            &[&username],
        )
        .await;

    match result {
        Ok(Some(row)) => {
            let user = User {
                id: Some(row.get("id")),
                username: row.get("username"),
                first_name: row.get("first_name"),
                last_name: row.get("last_name"),
                email: row.get("email"),
                password: row.get("password"),
                phone: row.get("phone"),
                user_status: row.get("user_status"),
            };
            HttpResponse::Ok().json(user)
        }
        Ok(None) => HttpResponse::NotFound().json(serde_json::json!({"message": "User not found"})),
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

    let result = client
        .query_opt(
            "UPDATE users SET username = COALESCE($1, username), first_name = $2, last_name = $3, email = $4, password = $5, phone = $6, user_status = $7 WHERE username = $8 RETURNING id, username, first_name, last_name, email, password, phone, user_status",
            &[&user.username, &user.first_name, &user.last_name, &user.email, &user.password, &user.phone, &user.user_status, &username],
        )
        .await;

    match result {
        Ok(Some(row)) => {
            let result = User {
                id: Some(row.get("id")),
                username: row.get("username"),
                first_name: row.get("first_name"),
                last_name: row.get("last_name"),
                email: row.get("email"),
                password: row.get("password"),
                phone: row.get("phone"),
                user_status: row.get("user_status"),
            };
            HttpResponse::Ok().json(result)
        }
        Ok(None) => HttpResponse::NotFound().json(serde_json::json!({"message": "User not found"})),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn delete_user(pool: web::Data<Pool>, path: web::Path<String>) -> HttpResponse {
    let username = path.into_inner();

    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let result = client
        .execute("DELETE FROM users WHERE username = $1", &[&username])
        .await;

    match result {
        Ok(count) if count > 0 => HttpResponse::Ok().json(serde_json::json!({"message": "successful operation"})),
        Ok(_) => HttpResponse::NotFound().json(serde_json::json!({"message": "User not found"})),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

async fn login_user(pool: web::Data<Pool>, query: web::Query<LoginQuery>) -> HttpResponse {
    let client = match pool.get().await {
        Ok(c) => c,
        Err(_) => return HttpResponse::InternalServerError().finish(),
    };

    let result = client
        .query_opt(
            "SELECT id FROM users WHERE username = $1 AND password = $2",
            &[&query.username, &query.password],
        )
        .await;

    match result {
        Ok(Some(_)) => {
            let token = format!("logged in user session:{}", chrono::Utc::now().timestamp());
            HttpResponse::Ok().json(token)
        }
        Ok(None) => HttpResponse::BadRequest().json(serde_json::json!({"message": "Invalid credentials"})),
        Err(_) => HttpResponse::InternalServerError().finish(),
    }
}

// ─── Main ───

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let db_host = env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string());
    let db_port = env::var("DB_PORT").unwrap_or_else(|_| "5432".to_string());
    let db_user = env::var("DB_USER").unwrap_or_else(|_| "postgres".to_string());
    let db_password = env::var("DB_PASSWORD").unwrap_or_else(|_| "postgres".to_string());
    let db_name = env::var("DB_NAME").unwrap_or_else(|_| "petstore".to_string());
    let port: u16 = env::var("PORT")
        .unwrap_or_else(|_| "5001".to_string())
        .parse()
        .unwrap_or(5001);

    let mut cfg = Config::new();
    cfg.host = Some(db_host);
    cfg.port = Some(db_port.parse().unwrap_or(5432));
    cfg.user = Some(db_user);
    cfg.password = Some(db_password);
    cfg.dbname = Some(db_name);

    let pool = cfg
        .create_pool(Some(Runtime::Tokio1), NoTls)
        .expect("Failed to create pool");

    init_db(&pool).await;

    let pool_data = web::Data::new(pool);

    HttpServer::new(move || {
        App::new()
            .app_data(pool_data.clone())
            // Pet endpoints
            .route("/pet", web::post().to(add_pet))
            .route("/pet", web::put().to(update_pet))
            .route("/pet/findByStatus", web::get().to(find_pets_by_status))
            .route("/pet/{petId}", web::get().to(get_pet_by_id))
            .route("/pet/{petId}", web::delete().to(delete_pet))
            // Store/Order endpoints
            .route("/store/order", web::post().to(place_order))
            .route("/store/order/{orderId}", web::get().to(get_order_by_id))
            .route("/store/order/{orderId}", web::delete().to(delete_order))
            // User endpoints
            .route("/user/login", web::get().to(login_user))
            .route("/user", web::post().to(create_user))
            .route("/user/{username}", web::get().to(get_user_by_name))
            .route("/user/{username}", web::put().to(update_user))
            .route("/user/{username}", web::delete().to(delete_user))
    })
    .bind(("0.0.0.0", port))?
    .run()
    .await
}