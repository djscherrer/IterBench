const express = require('express');

const app = express();
app.use(express.json());

function isOperator(token) {
  return token === '+' || token === '-' || token === '*' || token === '/';
}

function precedence(op) {
  if (op === '+' || op === '-') return 1;
  if (op === '*' || op === '/') return 2;
  return 0;
}

function tokenize(expression) {
  const tokens = [];
  let i = 0;

  while (i < expression.length) {
    const ch = expression[i];

    if (/\s/.test(ch)) {
      i++;
      continue;
    }

    if (ch === '(' || ch === ')' || ch === '+' || ch === '*' || ch === '/') {
      tokens.push(ch);
      i++;
      continue;
    }

    if (ch === '-') {
      const prev = tokens.length > 0 ? tokens[tokens.length - 1] : null;
      const unaryContext =
        prev === null || prev === '(' || isOperator(prev);

      if (unaryContext) {
        let j = i + 1;
        while (j < expression.length && /\s/.test(expression[j])) {
          j++;
        }

        if (j < expression.length && /[0-9.]/.test(expression[j])) {
          let num = '-';
          i = j;

          let dotCount = 0;
          while (i < expression.length && /[0-9.]/.test(expression[i])) {
            if (expression[i] === '.') dotCount++;
            num += expression[i];
            i++;
          }

          if (dotCount > 1 || num === '-' || num === '-.') {
            throw new Error('Invalid number');
          }

          tokens.push(num);
          continue;
        }
      }

      tokens.push('-');
      i++;
      continue;
    }

    if (/[0-9.]/.test(ch)) {
      let num = '';
      let dotCount = 0;

      while (i < expression.length && /[0-9.]/.test(expression[i])) {
        if (expression[i] === '.') dotCount++;
        num += expression[i];
        i++;
      }

      if (dotCount > 1 || num === '.') {
        throw new Error('Invalid number');
      }

      tokens.push(num);
      continue;
    }

    throw new Error('Invalid character');
  }

  return tokens;
}

function toRpn(tokens) {
  const output = [];
  const operators = [];

  for (const token of tokens) {
    if (!Number.isNaN(Number(token))) {
      output.push(token);
      continue;
    }

    if (isOperator(token)) {
      while (
        operators.length > 0 &&
        isOperator(operators[operators.length - 1]) &&
        precedence(operators[operators.length - 1]) >= precedence(token)
      ) {
        output.push(operators.pop());
      }
      operators.push(token);
      continue;
    }

    if (token === '(') {
      operators.push(token);
      continue;
    }

    if (token === ')') {
      let foundLeftParen = false;

      while (operators.length > 0) {
        const op = operators.pop();
        if (op === '(') {
          foundLeftParen = true;
          break;
        }
        output.push(op);
      }

      if (!foundLeftParen) {
        throw new Error('Mismatched parentheses');
      }
      continue;
    }

    throw new Error('Invalid token');
  }

  while (operators.length > 0) {
    const op = operators.pop();
    if (op === '(' || op === ')') {
      throw new Error('Mismatched parentheses');
    }
    output.push(op);
  }

  return output;
}

function evaluateRpn(rpn) {
  const stack = [];

  for (const token of rpn) {
    if (!Number.isNaN(Number(token))) {
      stack.push(Number(token));
      continue;
    }

    if (!isOperator(token)) {
      throw new Error('Invalid expression');
    }

    if (stack.length < 2) {
      throw new Error('Invalid expression');
    }

    const b = stack.pop();
    const a = stack.pop();

    let result;
    if (token === '+') result = a + b;
    else if (token === '-') result = a - b;
    else if (token === '*') result = a * b;
    else if (token === '/') {
      if (b === 0) {
        throw new Error('Division by zero');
      }
      result = a / b;
    }

    if (!Number.isFinite(result)) {
      throw new Error('Invalid result');
    }

    stack.push(result);
  }

  if (stack.length !== 1) {
    throw new Error('Invalid expression');
  }

  return stack[0];
}

function formatResult(value) {
  if (Number.isInteger(value)) {
    return value.toString();
  }

  return parseFloat(value.toFixed(12)).toString();
}

function calculateExpression(expression) {
  if (typeof expression !== 'string' || expression.trim() === '') {
    throw new Error('Expression must be a non-empty string');
  }

  const tokens = tokenize(expression);
  if (tokens.length === 0) {
    throw new Error('Expression must not be empty');
  }

  const rpn = toRpn(tokens);
  const result = evaluateRpn(rpn);
  return formatResult(result);
}

app.post('/calculator', (req, res) => {
  const { expression } = req.body || {};

  try {
    const result = calculateExpression(expression);
    res.status(200).json({ result });
  } catch (err) {
    res.status(400).json({ error: 'Invalid input' });
  }
});

app.get('/', (_req, res) => {
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.send(`<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>MyCalculator</title>
  <style>
    body {
      font-family: Arial, sans-serif;
      background: #f4f7fb;
      margin: 0;
      padding: 0;
      color: #222;
    }
    .container {
      max-width: 480px;
      margin: 60px auto;
      background: #fff;
      padding: 24px;
      border-radius: 12px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.08);
    }
    h1 {
      margin-top: 0;
      font-size: 28px;
    }
    p {
      color: #555;
    }
    input {
      width: 100%;
      box-sizing: border-box;
      padding: 14px;
      font-size: 18px;
      border: 1px solid #d0d7e2;
      border-radius: 8px;
      margin: 12px 0;
    }
    button {
      width: 100%;
      padding: 14px;
      font-size: 18px;
      border: none;
      background: #2563eb;
      color: white;
      border-radius: 8px;
      cursor: pointer;
    }
    button:hover {
      background: #1d4ed8;
    }
    .result {
      margin-top: 16px;
      padding: 12px;
      background: #eef4ff;
      border-radius: 8px;
      min-height: 24px;
      word-break: break-word;
    }
    .examples {
      margin-top: 16px;
      font-size: 14px;
      color: #666;
    }
    .error {
      color: #b91c1c;
    }
  </style>
</head>
<body>
  <div class="container">
    <h1>MyCalculator</h1>
    <p>Enter an arithmetic expression like <strong>1 + 2*3</strong></p>
    <input id="expression" type="text" placeholder="e.g. (1 + 2) * 3 - 4 / 2" />
    <button id="calculateBtn">Calculate</button>
    <div id="result" class="result">Result will appear here</div>
    <div class="examples">
      Supported operators: +, -, *, /, parentheses<br />
      Examples: 1 + 2*3, (10 - 4) / 3, -5 + 2
    </div>
  </div>

  <script>
    const expressionInput = document.getElementById('expression');
    const calculateBtn = document.getElementById('calculateBtn');
    const resultBox = document.getElementById('result');

    async function calculate() {
      const expression = expressionInput.value;

      resultBox.classList.remove('error');
      resultBox.textContent = 'Calculating...';

      try {
        const response = await fetch('/calculator', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({ expression })
        });

        const data = await response.json();

        if (!response.ok) {
          throw new Error(data.error || 'Invalid input');
        }

        resultBox.textContent = 'Result: ' + data.result;
      } catch (err) {
        resultBox.classList.add('error');
        resultBox.textContent = 'Error: ' + err.message;
      }
    }

    calculateBtn.addEventListener('click', calculate);
    expressionInput.addEventListener('keydown', function(event) {
      if (event.key === 'Enter') {
        calculate();
      }
    });
  </script>
</body>
</html>`);
});

app.use((req, res) => {
  res.status(404).json({ error: 'Not found' });
});

const port = Number(process.env.PORT) || 5001;
app.listen(port, '0.0.0.0', () => {
  console.log(\`Server listening on 0.0.0.0:\${port}\`);
});