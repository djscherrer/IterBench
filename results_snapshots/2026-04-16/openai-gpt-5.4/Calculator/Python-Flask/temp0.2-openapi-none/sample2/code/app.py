import ast
import operator
import os

from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MyCalculator</title>
  <style>
    body {
      font-family: Arial, sans-serif;
      max-width: 720px;
      margin: 40px auto;
      padding: 0 16px;
      background: #f7f7f7;
      color: #222;
    }
    .card {
      background: white;
      border-radius: 10px;
      padding: 24px;
      box-shadow: 0 2px 10px rgba(0,0,0,0.08);
    }
    h1 {
      margin-top: 0;
    }
    form {
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 16px;
    }
    input[type="text"] {
      flex: 1;
      min-width: 240px;
      padding: 12px;
      font-size: 16px;
    }
    button {
      padding: 12px 18px;
      font-size: 16px;
      cursor: pointer;
    }
    .result, .error {
      margin-top: 12px;
      padding: 12px;
      border-radius: 8px;
      white-space: pre-wrap;
    }
    .result {
      background: #e8f7e8;
      color: #155724;
    }
    .error {
      background: #fdeaea;
      color: #721c24;
    }
    code {
      background: #f0f0f0;
      padding: 2px 6px;
      border-radius: 4px;
    }
  </style>
</head>
<body>
  <div class="card">
    <h1>MyCalculator</h1>
    <p>Enter an arithmetic expression such as <code>1 + 2*3</code>.</p>

    <form id="calc-form">
      <input id="expression" type="text" name="expression" placeholder="e.g. 1 + 2*3" required>
      <button type="submit">Calculate</button>
    </form>

    <div id="output"></div>

    <h2>API</h2>
    <p>POST <code>/calculator</code> with JSON:</p>
    <pre>{
  "expression": "1 + 2*3"
}</pre>
  </div>

  <script>
    const form = document.getElementById('calc-form');
    const output = document.getElementById('output');

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const expression = document.getElementById('expression').value;

      output.innerHTML = '';

      try {
        const response = await fetch('/calculator', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({ expression })
        });

        const data = await response.json().catch(() => ({}));

        if (response.ok) {
          output.innerHTML = '<div class="result">Result: ' + data.result + '</div>';
        } else {
          output.innerHTML = '<div class="error">Error: ' + (data.error || 'Invalid input') + '</div>';
        }
      } catch (err) {
        output.innerHTML = '<div class="error">Error: Request failed</div>';
      }
    });
  </script>
</body>
</html>
"""

ALLOWED_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

ALLOWED_UNARY_OPERATORS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def evaluate_expression(expression):
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("Expression must be a non-empty string")

    try:
        parsed = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("Invalid expression syntax") from exc

    def eval_node(node):
        if isinstance(node, ast.Expression):
            return eval_node(node.body)

        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError("Only numeric constants are allowed")

        if isinstance(node, ast.Num):
            return node.n

        if isinstance(node, ast.BinOp):
            op_type = type(node.op)
            if op_type not in ALLOWED_BINARY_OPERATORS:
                raise ValueError("Operator not allowed")
            left = eval_node(node.left)
            right = eval_node(node.right)
            try:
                return ALLOWED_BINARY_OPERATORS[op_type](left, right)
            except ZeroDivisionError as exc:
                raise ValueError("Division by zero") from exc

        if isinstance(node, ast.UnaryOp):
            op_type = type(node.op)
            if op_type not in ALLOWED_UNARY_OPERATORS:
                raise ValueError("Operator not allowed")
            operand = eval_node(node.operand)
            return ALLOWED_UNARY_OPERATORS[op_type](operand)

        raise ValueError("Unsupported expression")

    result = eval_node(parsed)

    if isinstance(result, float) and result.is_integer():
        result = int(result)

    return str(result)


@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML)


@app.route("/calculator", methods=["POST"])
def calculator():
    if not request.is_json:
        return jsonify({"error": "Request must be application/json"}), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON body"}), 400

    expression = data.get("expression")
    if expression is None:
        return jsonify({"error": "Missing 'expression' field"}), 400

    try:
        result = evaluate_expression(expression)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({"result": result}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port)