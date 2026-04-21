use actix_web::{web, App, HttpResponse, HttpServer, Responder};
use serde::{Deserialize, Serialize};
use std::env;

#[derive(Debug, Deserialize)]
struct CalculationRequest {
    expression: String,
}

#[derive(Debug, Serialize)]
struct CalculationResponse {
    result: String,
}

fn calculate_expression(expression: &str) -> Result<f64, String> {
    // Remove whitespace
    let expr = expression.replace(' ', "");
    
    if expr.is_empty() {
        return Err("Empty expression".to_string());
    }

    // Simple expression evaluator that handles +, -, *, /
    let mut numbers: Vec<f64> = Vec::new();
    let mut operators: Vec<char> = Vec::new();
    let mut current_number = String::new();

    for ch in expr.chars() {
        if ch.is_digit(10) || ch == '.' {
            current_number.push(ch);
        } else if "+-*/".contains(ch) {
            if current_number.is_empty() {
                return Err("Invalid expression: operator without preceding number".to_string());
            }
            
            match current_number.parse::<f64>() {
                Ok(num) => numbers.push(num),
                Err(_) => return Err(format!("Invalid number: {}", current_number)),
            }
            current_number.clear();
            
            // Handle operator precedence
            while !operators.is_empty() && has_precedence(ch, *operators.last().unwrap()) {
                if let Err(e) = apply_operation(&mut numbers, &mut operators) {
                    return Err(e);
                }
            }
            operators.push(ch);
        } else {
            return Err(format!("Invalid character in expression: {}", ch));
        }
    }

    // Push the last number
    if current_number.is_empty() {
        return Err("Invalid expression: ends with operator".to_string());
    }
    
    match current_number.parse::<f64>() {
        Ok(num) => numbers.push(num),
        Err(_) => return Err(format!("Invalid number: {}", current_number)),
    }

    // Apply remaining operators
    while !operators.is_empty() {
        if let Err(e) = apply_operation(&mut numbers, &mut operators) {
            return Err(e);
        }
    }

    if numbers.len() != 1 {
        return Err("Invalid expression format".to_string());
    }

    Ok(numbers[0])
}

fn has_precedence(op1: char, op2: char) -> bool {
    // * and / have higher precedence than + and -
    (op2 == '*' || op2 == '/') && (op1 == '+' || op1 == '-')
}

fn apply_operation(numbers: &mut Vec<f64>, operators: &mut Vec<char>) -> Result<(), String> {
    if numbers.len() < 2 || operators.is_empty() {
        return Err("Invalid expression: insufficient operands".to_string());
    }

    let op = operators.pop().unwrap();
    let b = numbers.pop().unwrap();
    let a = numbers.pop().unwrap();

    let result = match op {
        '+' => a + b,
        '-' => a - b,
        '*' => a * b,
        '/' => {
            if b == 0.0 {
                return Err("Division by zero".to_string());
            }
            a / b
        }
        _ => return Err(format!("Unknown operator: {}", op)),
    };

    numbers.push(result);
    Ok(())
}

async fn calculate(
    request: web::Json<CalculationRequest>,
) -> impl Responder {
    match calculate_expression(&request.expression) {
        Ok(result) => HttpResponse::Ok().json(CalculationResponse {
            result: result.to_string(),
        }),
        Err(err) => HttpResponse::BadRequest().body(err),
    }
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    // Initialize logger
    env_logger::init_from_env(env_logger::Env::new().default_filter_or("info"));

    // Get port from environment variable or default to 5001
    let port = env::var("PORT")
        .unwrap_or_else(|_| "5001".to_string())
        .parse::<u16>()
        .unwrap_or(5001);

    let bind_address = format!("0.0.0.0:{}", port);
    
    println!("Starting server on {}", bind_address);

    HttpServer::new(|| {
        App::new()
            .service(
                web::resource("/calculator")
                    .route(web::post().to(calculate))
            )
    })
    .bind(bind_address)?
    .run()
    .await
}