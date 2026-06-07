use actix_web::{post, web, App, HttpResponse, HttpServer, Responder};
use serde::{Deserialize, Serialize};
use std::env;

#[derive(Deserialize)]
struct CalculatorRequest {
    expression: String,
}

#[derive(Serialize)]
struct CalculatorResponse {
    result: String,
}

fn evaluate_expression(expr: &str) -> Result<f64, String> {
    // Remove whitespace
    let expr = expr.replace(' ', "");
    if expr.is_empty() {
        return Err("Empty expression".to_string());
    }

    let mut numbers: Vec<f64> = Vec::new();
    let mut operators: Vec<char> = Vec::new();
    let mut current_number = String::new();

    let mut i = 0;
    let chars: Vec<char> = expr.chars().collect();
    let n = chars.len();

    while i < n {
        let c = chars[i];

        if c.is_ascii_digit() || c == '.' {
            current_number.push(c);
            i += 1;
        } else if "+-*/".contains(c) {
            if current_number.is_empty() && c == '-' {
                // Handle unary minus
                current_number.push('-');
                i += 1;
                continue;
            }

            if current_number.is_empty() {
                return Err("Invalid expression: operator without preceding number".to_string());
            }

            // Parse the current number
            match current_number.parse::<f64>() {
                Ok(num) => numbers.push(num),
                Err(_) => return Err(format!("Invalid number: {}", current_number)),
            }
            current_number.clear();

            // Process operators based on precedence
            while !operators.is_empty() && has_precedence(c, *operators.last().unwrap()) {
                if let Err(e) = apply_operation(&mut numbers, &mut operators) {
                    return Err(e);
                }
            }
            operators.push(c);
            i += 1;
        } else {
            return Err(format!("Invalid character: {}", c));
        }
    }

    // Parse the last number
    if !current_number.is_empty() {
        match current_number.parse::<f64>() {
            Ok(num) => numbers.push(num),
            Err(_) => return Err(format!("Invalid number: {}", current_number)),
        }
    }

    // Apply remaining operators
    while !operators.is_empty() {
        if let Err(e) = apply_operation(&mut numbers, &mut operators) {
            return Err(e);
        }
    }

    if numbers.len() != 1 {
        return Err("Invalid expression".to_string());
    }

    Ok(numbers[0])
}

fn has_precedence(op1: char, op2: char) -> bool {
    // Returns true if op2 has higher or equal precedence than op1
    match (op1, op2) {
        ('*' | '/', '+' | '-') => true,
        ('*' | '/', '*' | '/') => true,
        ('+' | '-', '+' | '-') => true,
        _ => false,
    }
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
        _ => return Err(format!("Invalid operator: {}", op)),
    };

    numbers.push(result);
    Ok(())
}

#[post("/calculator")]
async fn calculate(req_body: web::Json<CalculatorRequest>) -> impl Responder {
    match evaluate_expression(&req_body.expression) {
        Ok(result) => HttpResponse::Ok().json(CalculatorResponse {
            result: result.to_string(),
        }),
        Err(err) => HttpResponse::BadRequest().body(err),
    }
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let port = env::var("PORT")
        .unwrap_or_else(|_| "5001".to_string())
        .parse::<u16>()
        .unwrap_or(5001);

    let server = HttpServer::new(|| App::new().service(calculate))
        .bind(("0.0.0.0", port))?
        .run();

    println!("Server running on 0.0.0.0:{}", port);
    server.await
}