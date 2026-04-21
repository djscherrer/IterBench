import ast
import operator
import os

from flask import Flask, jsonify, request

app = Flask(__name__)


class SafeEvaluator:
    BIN_OPS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }

    UNARY_OPS = {
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
    }

    def evaluate(self, expression: str):
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError("Expression must be a non-empty string")

        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise ValueError("Invalid expression") from exc

        return self._eval_node(tree.body)

    def _eval_node(self, node):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError("Only numeric constants are allowed")

        if isinstance(node, ast.Num):  # compatibility
            return node.n

        if isinstance(node, ast.BinOp):
            op_type = type(node.op)
            if op_type not in self.BIN_OPS:
                raise ValueError("Operator not allowed")
            left = self._eval_node(node.left)
            right = self._eval_node(node.right)
            try:
                return self.BIN_OPS[op_type](left, right)
            except ZeroDivisionError as exc:
                raise ValueError("Division by zero") from exc

        if isinstance(node, ast.UnaryOp):
            op_type = type(node.op)
            if op_type not in self.UNARY_OPS:
                raise ValueError("Operator not allowed")
            operand = self._eval_node(node.operand)
            return self.UNARY_OPS[op_type](operand)

        raise ValueError("Invalid expression")


evaluator = SafeEvaluator()


@app.get("/")
def index():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MyCalculator</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            max-width: 640px;
            margin: 40px auto;
            padding: 0 16px;
            background: #f7f7f7;
            color: #222;
        }
        .card {
            background: #fff;
            border-radius: 8px;
            padding: 24px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.08);
        }
        h1 {
            margin-top: 0;
        }
        input, button {
            font-size: 16px;
            padding: 10px 12px;
        }
        input {
            width: 100%;
            box-sizing: border-box;
            margin-bottom: 12px;
        }
        button {
            cursor: pointer;
        }
        .result, .error {
            margin-top: 16px;
            padding: 12px;
            border-radius: 6px;
        }
        .result {
            background: #e8f7e8;
        }
        .error {
            background: #fde8e8;
            color: #8a1f1f;
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
        <p>Enter an arithmetic expression like <code>1 + 2*3</code>.</p>
        <input id="expression" type="text" placeholder="e.g. 1 + 2*3" />
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

                const data = await response.json().catch(() => ({}));

                if (response.ok) {
                    output.innerHTML = '<div class="result"><strong>Result:</strong> ' + data.result + '</div>';
                } else {
                    output.innerHTML = '<div class="error"><strong>Error:</strong> ' + (data.error || 'Invalid input') + '</div>';
                }
            } catch (err) {
                output.innerHTML = '<div class="error"><strong>Error:</strong> Request failed</div>';
            }
        }
    </script>
</body>
</html>
"""


@app.post("/calculator")
def calculate():
    if not request.is_json:
        return jsonify({"error": "Invalid input"}), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or "expression" not in data:
        return jsonify({"error": "Invalid input"}), 400

    expression = data.get("expression")
    try:
        result = evaluator.evaluate(expression)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({"result": str(result)}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port)