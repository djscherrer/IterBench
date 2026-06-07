use actix_web::{web, App, HttpResponse, HttpServer, Responder};
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

#[derive(Debug, Clone, PartialEq)]
enum Token {
    Number(f64),
    Plus,
    Minus,
    Multiply,
    Divide,
    LParen,
    RParen,
}

fn tokenize(input: &str) -> Result<Vec<Token>, String> {
    let chars: Vec<char> = input.chars().collect();
    let mut tokens = Vec::new();
    let mut i = 0;

    while i < chars.len() {
        match chars[i] {
            ' ' | '\t' | '\n' | '\r' => {
                i += 1;
            }
            '+' => {
                tokens.push(Token::Plus);
                i += 1;
            }
            '-' => {
                tokens.push(Token::Minus);
                i += 1;
            }
            '*' => {
                tokens.push(Token::Multiply);
                i += 1;
            }
            '/' => {
                tokens.push(Token::Divide);
                i += 1;
            }
            '(' => {
                tokens.push(Token::LParen);
                i += 1;
            }
            ')' => {
                tokens.push(Token::RParen);
                i += 1;
            }
            c if c.is_ascii_digit() || c == '.' => {
                let start = i;
                let mut dot_count = 0;

                while i < chars.len() && (chars[i].is_ascii_digit() || chars[i] == '.') {
                    if chars[i] == '.' {
                        dot_count += 1;
                        if dot_count > 1 {
                            return Err("Invalid number format".to_string());
                        }
                    }
                    i += 1;
                }

                let number_str: String = chars[start..i].iter().collect();
                let number = number_str
                    .parse::<f64>()
                    .map_err(|_| "Invalid number".to_string())?;
                tokens.push(Token::Number(number));
            }
            _ => {
                return Err(format!("Invalid character: {}", chars[i]));
            }
        }
    }

    Ok(tokens)
}

struct Parser {
    tokens: Vec<Token>,
    pos: usize,
}

impl Parser {
    fn new(tokens: Vec<Token>) -> Self {
        Self { tokens, pos: 0 }
    }

    fn parse(&mut self) -> Result<f64, String> {
        let result = self.parse_expression()?;
        if self.pos != self.tokens.len() {
            return Err("Unexpected trailing tokens".to_string());
        }
        Ok(result)
    }

    fn parse_expression(&mut self) -> Result<f64, String> {
        let mut value = self.parse_term()?;

        while self.pos < self.tokens.len() {
            match self.tokens[self.pos] {
                Token::Plus => {
                    self.pos += 1;
                    value += self.parse_term()?;
                }
                Token::Minus => {
                    self.pos += 1;
                    value -= self.parse_term()?;
                }
                _ => break,
            }
        }

        Ok(value)
    }

    fn parse_term(&mut self) -> Result<f64, String> {
        let mut value = self.parse_factor()?;

        while self.pos < self.tokens.len() {
            match self.tokens[self.pos] {
                Token::Multiply => {
                    self.pos += 1;
                    value *= self.parse_factor()?;
                }
                Token::Divide => {
                    self.pos += 1;
                    let divisor = self.parse_factor()?;
                    if divisor == 0.0 {
                        return Err("Division by zero".to_string());
                    }
                    value /= divisor;
                }
                _ => break,
            }
        }

        Ok(value)
    }

    fn parse_factor(&mut self) -> Result<f64, String> {
        if self.pos >= self.tokens.len() {
            return Err("Unexpected end of expression".to_string());
        }

        match self.tokens[self.pos].clone() {
            Token::Number(n) => {
                self.pos += 1;
                Ok(n)
            }
            Token::Minus => {
                self.pos += 1;
                Ok(-self.parse_factor()?)
            }
            Token::Plus => {
                self.pos += 1;
                self.parse_factor()
            }
            Token::LParen => {
                self.pos += 1;
                let value = self.parse_expression()?;
                if self.pos >= self.tokens.len() || self.tokens[self.pos] != Token::RParen {
                    return Err("Missing closing parenthesis".to_string());
                }
                self.pos += 1;
                Ok(value)
            }
            _ => Err("Expected number or parenthesized expression".to_string()),
        }
    }
}

fn evaluate_expression(expression: &str) -> Result<f64, String> {
    let tokens = tokenize(expression)?;
    if tokens.is_empty() {
        return Err("Expression cannot be empty".to_string());
    }
    let mut parser = Parser::new(tokens);
    parser.parse()
}

fn format_result(value: f64) -> String {
    if value.fract() == 0.0 {
        format!("{}", value as i64)
    } else {
        let mut s = format!("{}", value);
        if s.contains('.') {
            while s.ends_with('0') {
                s.pop();
            }
            if s.ends_with('.') {
                s.pop();
            }
        }
        s
    }
}

async fn calculate(req: web::Json<CalculatorRequest>) -> impl Responder {
    match evaluate_expression(&req.expression) {
        Ok(result) => HttpResponse::Ok().json(CalculatorResponse {
            result: format_result(result),
        }),
        Err(_) => HttpResponse::BadRequest().finish(),
    }
}

async fn index() -> impl Responder {
    let html = r#"<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MyCalculator</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            margin: 40px;
            max-width: 720px;
            line-height: 1.5;
        }
        h1 {
            margin-bottom: 8px;
        }
        .container {
            border: 1px solid #ddd;
            border-radius: 8px;
            padding: 20px;
            background: #fafafa;
        }
        input[type="text"] {
            width: 100%;
            box-sizing: border-box;
            padding: 12px;
            font-size: 16px;
            margin: 12px 0;
        }
        button {
            padding: 10px 16px;
            font-size: 16px;
            cursor: pointer;
        }
        .result, .error {
            margin-top: 16px;
            padding: 12px;
            border-radius: 6px;
        }
        .result {
            background: #e8f5e9;
            color: #1b5e20;
        }
        .error {
            background: #ffebee;
            color: #b71c1c;
        }
        code {
            background: #eee;
            padding: 2px 4px;
            border-radius: 4px;
        }
    </style>
</head>
<body>
    <h1>MyCalculator</h1>
    <p>Enter an arithmetic expression like <code>1 + 2*3</code> and click calculate.</p>
    <div class="container">
        <input id="expression" type="text" placeholder="e.g. (1 + 2) * 3 - 4 / 2" />
        <button onclick="calculate()">Calculate</button>
        <div id="output"></div>
    </div>

    <script>
        async function calculate() {
            const expression = document.getElementById('expression').value;
            const output = document.getElementById('output');
            output.innerHTML = '';

            try {
                const response = await fetch('/calculator', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ expression })
                });

                if (response.ok) {
                    const data = await response.json();
                    output.innerHTML = '<div class="result"><strong>Result:</strong> ' + data.result + '</div>';
                } else {
                    output.innerHTML = '<div class="error">Invalid input</div>';
                }
            } catch (err) {
                output.innerHTML = '<div class="error">Request failed</div>';
            }
        }

        document.getElementById('expression').addEventListener('keydown', function(event) {
            if (event.key === 'Enter') {
                calculate();
            }
        });
    </script>
</body>
</html>"#;

    HttpResponse::Ok().content_type("text/html; charset=utf-8").body(html)
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    env_logger::init();

    let port: u16 = env::var("PORT")
        .ok()
        .and_then(|p| p.parse::<u16>().ok())
        .unwrap_or(5001);

    HttpServer::new(|| {
        App::new()
            .route("/", web::get().to(index))
            .route("/calculator", web::post().to(calculate))
    })
    .bind(("0.0.0.0", port))?
    .run()
    .await
}