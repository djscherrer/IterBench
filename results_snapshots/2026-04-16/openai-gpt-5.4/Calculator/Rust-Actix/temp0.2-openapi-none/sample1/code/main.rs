use actix_web::{web, App, HttpResponse, HttpServer, Responder};
use serde::{Deserialize, Serialize};
use std::env;

#[derive(Deserialize)]
struct CalculateRequest {
    expression: String,
}

#[derive(Serialize)]
struct CalculateResponse {
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

#[derive(Debug)]
enum CalcError {
    InvalidInput(String),
}

impl CalcError {
    fn message(&self) -> &str {
        match self {
            CalcError::InvalidInput(msg) => msg.as_str(),
        }
    }
}

fn tokenize(input: &str) -> Result<Vec<Token>, CalcError> {
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
                            return Err(CalcError::InvalidInput("Invalid number format".to_string()));
                        }
                    }
                    i += 1;
                }

                let number_str: String = chars[start..i].iter().collect();
                let number = number_str
                    .parse::<f64>()
                    .map_err(|_| CalcError::InvalidInput("Invalid number".to_string()))?;
                tokens.push(Token::Number(number));
            }
            _ => {
                return Err(CalcError::InvalidInput("Expression contains invalid characters".to_string()));
            }
        }
    }

    if tokens.is_empty() {
        return Err(CalcError::InvalidInput("Expression cannot be empty".to_string()));
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

    fn parse(&mut self) -> Result<f64, CalcError> {
        let value = self.parse_expression()?;
        if self.pos != self.tokens.len() {
            return Err(CalcError::InvalidInput("Unexpected token at end of expression".to_string()));
        }
        Ok(value)
    }

    fn parse_expression(&mut self) -> Result<f64, CalcError> {
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

    fn parse_term(&mut self) -> Result<f64, CalcError> {
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
                        return Err(CalcError::InvalidInput("Division by zero".to_string()));
                    }
                    value /= divisor;
                }
                _ => break,
            }
        }

        Ok(value)
    }

    fn parse_factor(&mut self) -> Result<f64, CalcError> {
        if self.pos >= self.tokens.len() {
            return Err(CalcError::InvalidInput("Unexpected end of expression".to_string()));
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
                    return Err(CalcError::InvalidInput("Missing closing parenthesis".to_string()));
                }
                self.pos += 1;
                Ok(value)
            }
            _ => Err(CalcError::InvalidInput("Unexpected token".to_string())),
        }
    }
}

fn evaluate_expression(expression: &str) -> Result<String, CalcError> {
    let tokens = tokenize(expression)?;
    let mut parser = Parser::new(tokens);
    let result = parser.parse()?;

    if !result.is_finite() {
        return Err(CalcError::InvalidInput("Result is not finite".to_string()));
    }

    if result.fract() == 0.0 {
        Ok(format!("{}", result as i64))
    } else {
        Ok(result.to_string())
    }
}

async fn calculate(req: web::Json<CalculateRequest>) -> impl Responder {
    match evaluate_expression(&req.expression) {
        Ok(result) => HttpResponse::Ok().json(CalculateResponse { result }),
        Err(err) => HttpResponse::BadRequest().body(err.message().to_string()),
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
            max-width: 500px;
            margin: 40px auto;
            padding: 20px;
            background: #f5f5f5;
        }
        .card {
            background: white;
            padding: 24px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        h1 {
            margin-top: 0;
        }
        input[type="text"] {
            width: 100%;
            box-sizing: border-box;
            padding: 12px;
            font-size: 16px;
            margin-bottom: 12px;
        }
        button {
            padding: 12px 16px;
            font-size: 16px;
            cursor: pointer;
        }
        .result, .error {
            margin-top: 16px;
            padding: 12px;
            border-radius: 4px;
        }
        .result {
            background: #e8f5e9;
            color: #1b5e20;
        }
        .error {
            background: #ffebee;
            color: #b71c1c;
        }
        .hint {
            color: #555;
            font-size: 14px;
            margin-top: 8px;
        }
        code {
            background: #eee;
            padding: 2px 4px;
            border-radius: 4px;
        }
    </style>
</head>
<body>
    <div class="card">
        <h1>MyCalculator</h1>
        <p>Enter an arithmetic expression to calculate its result.</p>
        <input id="expression" type="text" placeholder="e.g. 1 + 2*3" />
        <button onclick="calculate()">Calculate</button>
        <div class="hint">Supported operators: <code>+</code>, <code>-</code>, <code>*</code>, <code>/</code>, parentheses</div>
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
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ expression })
                });

                if (response.ok) {
                    const data = await response.json();
                    output.innerHTML = '<div class="result"><strong>Result:</strong> ' + data.result + '</div>';
                } else {
                    const errorText = await response.text();
                    output.innerHTML = '<div class="error"><strong>Error:</strong> ' + errorText + '</div>';
                }
            } catch (err) {
                output.innerHTML = '<div class="error"><strong>Error:</strong> Request failed</div>';
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
    let port = env::var("PORT")
        .ok()
        .and_then(|v| v.parse::<u16>().ok())
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