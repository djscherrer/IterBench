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
    tokens: Vec<Token>,
    pos: usize,
}

#[derive(Debug, Clone)]
enum Token {
    Number(f64),
    Plus,
    Minus,
    Star,
    Slash,
    LParen,
    RParen,
}

fn tokenize(input: &str) -> Result<Vec<Token>, String> {
    let mut tokens = Vec::new();
    let chars: Vec<char> = input.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if c.is_whitespace() {
            i += 1;
            continue;
        }
        match c {
            '+' => { tokens.push(Token::Plus); i += 1; }
            '-' => { tokens.push(Token::Minus); i += 1; }
            '*' => { tokens.push(Token::Star); i += 1; }
            '/' => { tokens.push(Token::Slash); i += 1; }
            '(' => { tokens.push(Token::LParen); i += 1; }
            ')' => { tokens.push(Token::RParen); i += 1; }
            _ if c.is_ascii_digit() || c == '.' => {
                let start = i;
                while i < chars.len() && (chars[i].is_ascii_digit() || chars[i] == '.') {
                    i += 1;
                }
                let num_str: String = chars[start..i].iter().collect();
                let num: f64 = num_str.parse().map_err(|_| format!("Invalid number: {}", num_str))?;
                tokens.push(Token::Number(num));
            }
            _ => return Err(format!("Unexpected character: {}", c)),
        }
    }
    Ok(tokens)
}

impl Parser {
    fn new(tokens: Vec<Token>) -> Self {
        Parser { tokens, pos: 0 }
    }

    fn peek(&self) -> Option<&Token> {
        self.tokens.get(self.pos)
    }

    fn advance(&mut self) -> Option<Token> {
        if self.pos < self.tokens.len() {
            let t = self.tokens[self.pos].clone();
            self.pos += 1;
            Some(t)
        } else {
            None
        }
    }

    fn parse_expr(&mut self) -> Result<f64, String> {
        let mut left = self.parse_term()?;
        loop {
            match self.peek() {
                Some(Token::Plus) => { self.advance(); left += self.parse_term()?; }
                Some(Token::Minus) => { self.advance(); left -= self.parse_term()?; }
                _ => break,
            }
        }
        Ok(left)
    }

    fn parse_term(&mut self) -> Result<f64, String> {
        let mut left = self.parse_factor()?;
        loop {
            match self.peek() {
                Some(Token::Star) => { self.advance(); left *= self.parse_factor()?; }
                Some(Token::Slash) => {
                    self.advance();
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

    fn parse_factor(&mut self) -> Result<f64, String> {
        if let Some(Token::Minus) = self.peek() {
            self.advance();
            let val = self.parse_atom()?;
            Ok(-val)
        } else {
            self.parse_atom()
        }
    }

    fn parse_atom(&mut self) -> Result<f64, String> {
        match self.advance() {
            Some(Token::Number(n)) => Ok(n),
            Some(Token::LParen) => {
                let val = self.parse_expr()?;
                match self.advance() {
                    Some(Token::RParen) => Ok(val),
                    _ => Err("Expected closing parenthesis".to_string()),
                }
            }
            other => Err(format!("Unexpected token: {:?}", other)),
        }
    }
}

fn evaluate(expression: &str) -> Result<f64, String> {
    let tokens = tokenize(expression)?;
    if tokens.is_empty() {
        return Err("Empty expression".to_string());
    }
    let mut parser = Parser::new(tokens);
    let result = parser.parse_expr()?;
    if parser.pos != parser.tokens.len() {
        return Err("Unexpected tokens at end of expression".to_string());
    }
    Ok(result)
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

    match evaluate(&expression) {
        Ok(result) => {
            let resp = CalculatorResponse {
                result: format_result(result),
            };
            HttpResponse::Ok().json(resp)
        }
        Err(_) => HttpResponse::BadRequest().body("Invalid input"),
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