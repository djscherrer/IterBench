use actix_web::{web, App, HttpServer, HttpResponse, post};
use serde::{Deserialize, Serialize};
use std::env;

#[derive(Deserialize)]
struct CalculatorRequest {
    expression: Option<String>,
}

#[derive(Serialize)]
struct CalculatorResponse {
    result: String,
}

/// Simple expression parser supporting +, -, *, /, parentheses, and unary minus.
/// Grammar:
///   expr   = term (('+' | '-') term)*
///   term   = factor (('*' | '/') factor)*
///   factor = ['-'] atom
///   atom   = number | '(' expr ')'

struct Parser {
    tokens: Vec<char>,
    pos: usize,
}

impl Parser {
    fn new(input: &str) -> Self {
        let tokens: Vec<char> = input.chars().collect();
        Parser { tokens, pos: 0 }
    }

    fn skip_whitespace(&mut self) {
        while self.pos < self.tokens.len() && self.tokens[self.pos].is_whitespace() {
            self.pos += 1;
        }
    }

    fn peek(&mut self) -> Option<char> {
        self.skip_whitespace();
        if self.pos < self.tokens.len() {
            Some(self.tokens[self.pos])
        } else {
            None
        }
    }

    fn consume(&mut self) -> Option<char> {
        self.skip_whitespace();
        if self.pos < self.tokens.len() {
            let c = self.tokens[self.pos];
            self.pos += 1;
            Some(c)
        } else {
            None
        }
    }

    fn parse_number(&mut self) -> Result<f64, String> {
        self.skip_whitespace();
        let start = self.pos;
        while self.pos < self.tokens.len()
            && (self.tokens[self.pos].is_ascii_digit() || self.tokens[self.pos] == '.')
        {
            self.pos += 1;
        }
        if start == self.pos {
            return Err("Expected number".to_string());
        }
        let num_str: String = self.tokens[start..self.pos].iter().collect();
        num_str.parse::<f64>().map_err(|e| e.to_string())
    }

    fn parse_atom(&mut self) -> Result<f64, String> {
        if let Some(c) = self.peek() {
            if c == '(' {
                self.consume(); // consume '('
                let val = self.parse_expr()?;
                match self.consume() {
                    Some(')') => Ok(val),
                    _ => Err("Expected ')'".to_string()),
                }
            } else {
                self.parse_number()
            }
        } else {
            Err("Unexpected end of expression".to_string())
        }
    }

    fn parse_factor(&mut self) -> Result<f64, String> {
        if let Some(c) = self.peek() {
            if c == '-' {
                self.consume(); // consume '-'
                let val = self.parse_atom()?;
                Ok(-val)
            } else if c == '+' {
                self.consume(); // consume unary '+'
                self.parse_atom()
            } else {
                self.parse_atom()
            }
        } else {
            Err("Unexpected end of expression".to_string())
        }
    }

    fn parse_term(&mut self) -> Result<f64, String> {
        let mut left = self.parse_factor()?;
        loop {
            match self.peek() {
                Some('*') => {
                    self.consume();
                    let right = self.parse_factor()?;
                    left *= right;
                }
                Some('/') => {
                    self.consume();
                    let right = self.parse_factor()?;
                    if right == 0.0 {
                        return Err("Division by zero".to_string());
                    }
                    left /= right;
                }
                _ => break,
            }
        }
        Ok(left)
    }

    fn parse_expr(&mut self) -> Result<f64, String> {
        let mut left = self.parse_term()?;
        loop {
            match self.peek() {
                Some('+') => {
                    self.consume();
                    let right = self.parse_term()?;
                    left += right;
                }
                Some('-') => {
                    self.consume();
                    let right = self.parse_term()?;
                    left -= right;
                }
                _ => break,
            }
        }
        Ok(left)
    }

    fn parse(mut self) -> Result<f64, String> {
        let result = self.parse_expr()?;
        self.skip_whitespace();
        if self.pos != self.tokens.len() {
            return Err(format!("Unexpected character at position {}", self.pos));
        }
        Ok(result)
    }
}

fn format_result(val: f64) -> String {
    if val == val.floor() && val.is_finite() && val.abs() < 1e15 {
        format!("{}", val as i64)
    } else {
        format!("{}", val)
    }
}

#[post("/calculator")]
async fn calculator(body: web::Json<CalculatorRequest>) -> HttpResponse {
    let expression = match &body.expression {
        Some(expr) => expr.clone(),
        None => return HttpResponse::BadRequest().body("Missing expression"),
    };

    if expression.trim().is_empty() {
        return HttpResponse::BadRequest().body("Empty expression");
    }

    let parser = Parser::new(&expression);
    match parser.parse() {
        Ok(result) => {
            let response = CalculatorResponse {
                result: format_result(result),
            };
            HttpResponse::Ok().json(response)
        }
        Err(_e) => HttpResponse::BadRequest().body("Invalid input"),
    }
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    let port: u16 = env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(5001);

    HttpServer::new(|| {
        App::new()
            .service(calculator)
    })
    .bind(("0.0.0.0", port))?
    .run()
    .await
}