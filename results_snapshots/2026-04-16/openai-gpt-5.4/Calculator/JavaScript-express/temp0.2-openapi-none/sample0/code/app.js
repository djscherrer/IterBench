const express = require('express');

const app = express();
app.use(express.json());

function isAllowedExpression(expression) {
  return typeof expression === 'string' && /^[\d+\-*/().\s]+$/.test(expression);
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

    if (/[0-9.]/.test(ch)) {
      let num = ch;
      i++;

      while (i < expression.length && /[0-9.]/.test(expression[i])) {
        num += expression[i];
        i++;
      }

      if ((num.match(/\./g) || []).length > 1 || num === '.') {
        throw new Error('Invalid number');
      }

      tokens.push({ type: 'number', value: parseFloat(num) });
      continue;
    }

    if ('+-*/()'.includes(ch)) {
      tokens.push({ type: 'operator', value: ch });
      i++;
      continue;
    }

    throw new Error('Invalid character');
  }

  return tokens;
}

function toRpn(tokens) {
  const output = [];
  const operators = [];
  const precedence = { '+': 1, '-': 1, '*': 2, '/': 2 };

  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];

    if (token.type === 'number') {
      output.push(token);
      continue;
    }

    const op = token.value;

    if (op === '(') {
      operators.push(token);
      continue;
    }

    if (op === ')') {
      while (operators.length && operators[operators.length - 1].value !== '(') {
        output.push(operators.pop());
      }

      if (!operators.length || operators[operators.length - 1].value !== '(') {
        throw new Error('Mismatched parentheses');
      }

      operators.pop();
      continue;
    }

    const prevToken = tokens[i - 1];
    const isUnaryMinus =
      op === '-' &&
      (
        i === 0 ||
        (prevToken.type === 'operator' && prevToken.value !== ')')
      );

    if (isUnaryMinus) {
      output.push({ type: 'number', value: 0 });
    }

    while (
      operators.length &&
      operators[operators.length - 1].value !== '(' &&
      precedence[operators[operators.length - 1].value] >= precedence[op]
    ) {
      output.push(operators.pop());
    }

    operators.push(token);
  }

  while (operators.length) {
    const op = operators.pop();
    if (op.value === '(' || op.value === ')') {
      throw new Error('Mismatched parentheses');
    }
    output.push(op);
  }

  return output;
}

function evalRpn(rpn) {
  const stack = [];

  for (const token of rpn) {
    if (token.type === 'number') {
      stack.push(token.value);
      continue;
    }

    const b = stack.pop();
    const a = stack.pop();

    if (a === undefined || b === undefined) {
      throw new Error('Invalid expression');
    }

    switch (token.value) {
      case '+':
        stack.push(a + b);
        break;
      case '-':
        stack.push(a - b);
        break;
      case '*':
        stack.push(a * b);
        break;
      case '/':
        if (b === 0) {
          throw new Error('Division by zero');
        }
        stack.push(a / b);
        break;
      default:
        throw new Error('Invalid operator');
    }
  }

  if (stack.length !== 1 || !Number.isFinite(stack[0])) {
    throw new Error('Invalid expression');
  }

  return stack[0];
}

function formatResult(value) {
  if (Number.isInteger(value)) {
    return String(value);
  }
  return String(value);
}

function calculateExpression(expression) {
  if (!isAllowedExpression(expression)) {
    throw new Error('Invalid input');
  }

  const tokens = tokenize(expression);
  if (tokens.length === 0) {
    throw new Error('Empty expression');
  }

  const rpn = toRpn(tokens);
  const result = evalRpn(rpn);
  return formatResult(result);
}

app.post('/calculator', (req, res) => {
  const { expression } = req.body || {};

  if (typeof expression !== 'string') {
    return res.status(400).json({ error: 'Invalid input' });
  }

  try {
    const result = calculateExpression(expression);
    return res.status(200).json({ result });
  } catch (err) {
    return res.status(400).json({ error: 'Invalid input' });
  }
});

app.use((req, res) => {
  res.status(404).json({ error: 'Not found' });
});

const PORT = parseInt(process.env.PORT, 10) || 5001;
app.listen(PORT, '0.0.0.0', () => {
  console.log(`Calculator app listening on 0.0.0.0:${PORT}`);
});