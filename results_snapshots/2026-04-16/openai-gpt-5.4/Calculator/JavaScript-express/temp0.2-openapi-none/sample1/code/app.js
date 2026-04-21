const express = require('express');

const app = express();
app.use(express.json());

function tokenize(expression) {
  const tokens = [];
  let i = 0;

  while (i &lt; expression.length) {
    const ch = expression[i];

    if (/\s/.test(ch)) {
      i++;
      continue;
    }

    if (/[0-9.]/.test(ch)) {
      let num = '';
      let dotCount = 0;

      while (i &lt; expression.length &amp;&amp; /[0-9.]/.test(expression[i])) {
        if (expression[i] === '.') {
          dotCount++;
          if (dotCount &gt; 1) {
            throw new Error('Invalid number');
          }
        }
        num += expression[i];
        i++;
      }

      if (num === '.' || num.length === 0) {
        throw new Error('Invalid number');
      }

      tokens.push({ type: 'number', value: Number(num) });

      if (!Number.isFinite(Number(num))) {
        throw new Error('Invalid number');
      }

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
  const precedence = {
    '+': 1,
    '-': 1,
    '*': 2,
    '/': 2,
    'u+': 3,
    'u-': 3
  };
  const rightAssociative = new Set(['u+', 'u-']);

  let prevType = 'start';

  for (const token of tokens) {
    if (token.type === 'number') {
      output.push(token);
      prevType = 'number';
      continue;
    }

    const op = token.value;

    if (op === '(') {
      operators.push({ type: 'operator', value: op });
      prevType = 'leftParen';
      continue;
    }

    if (op === ')') {
      let foundLeftParen = false;
      while (operators.length &gt; 0) {
        const top = operators.pop();
        if (top.value === '(') {
          foundLeftParen = true;
          break;
        }
        output.push(top);
      }
      if (!foundLeftParen) {
        throw new Error('Mismatched parentheses');
      }
      prevType = 'number';
      continue;
    }

    let currentOp = op;
    const unaryContext =
      prevType === 'start' ||
      prevType === 'operator' ||
      prevType === 'leftParen';

    if ((op === '+' || op === '-') &amp;&amp; unaryContext) {
      currentOp = op === '+' ? 'u+' : 'u-';
    } else if (unaryContext) {
      throw new Error('Invalid operator placement');
    }

    while (operators.length &gt; 0) {
      const top = operators[operators.length - 1];
      if (top.value === '(') {
        break;
      }

      const topPrec = precedence[top.value];
      const currentPrec = precedence[currentOp];

      if (
        topPrec &gt; currentPrec ||
        (topPrec === currentPrec &amp;&amp; !rightAssociative.has(currentOp))
      ) {
        output.push(operators.pop());
      } else {
        break;
      }
    }

    operators.push({ type: 'operator', value: currentOp });
    prevType = 'operator';
  }

  while (operators.length &gt; 0) {
    const top = operators.pop();
    if (top.value === '(' || top.value === ')') {
      throw new Error('Mismatched parentheses');
    }
    output.push(top);
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

    const op = token.value;

    if (op === 'u+' || op === 'u-') {
      if (stack.length &lt; 1) {
        throw new Error('Invalid expression');
      }
      const a = stack.pop();
      stack.push(op === 'u-' ? -a : a);
      continue;
    }

    if (stack.length &lt; 2) {
      throw new Error('Invalid expression');
    }

    const b = stack.pop();
    const a = stack.pop();
    let result;

    switch (op) {
      case '+':
        result = a + b;
        break;
      case '-':
        result = a - b;
        break;
      case '*':
        result = a * b;
        break;
      case '/':
        if (b === 0) {
          throw new Error('Division by zero');
        }
        result = a / b;
        break;
      default:
        throw new Error('Invalid operator');
    }

    if (!Number.isFinite(result)) {
      throw new Error('Invalid calculation');
    }

    stack.push(result);
  }

  if (stack.length !== 1) {
    throw new Error('Invalid expression');
  }

  return stack[0];
}

function calculateExpression(expression) {
  if (typeof expression !== 'string' || expression.trim() === '') {
    throw new Error('Expression is required');
  }

  const tokens = tokenize(expression);
  const rpn = toRpn(tokens);
  return evalRpn(rpn);
}

function formatResult(value) {
  if (Number.isInteger(value)) {
    return String(value);
  }
  return String(value);
}

app.post('/calculator', (req, res) => {
  const { expression } = req.body || {};

  try {
    const result = calculateExpression(expression);
    return res.status(200).json({
      result: formatResult(result)
    });
  } catch (err) {
    return res.status(400).json({
      error: 'Invalid input'
    });
  }
});

app.use((req, res) => {
  res.status(404).json({ error: 'Not found' });
});

const PORT = parseInt(process.env.PORT, 10) || 5001;
app.listen(PORT, '0.0.0.0', () => {
  console.log(`Calculator server listening on 0.0.0.0:${PORT}`);
});