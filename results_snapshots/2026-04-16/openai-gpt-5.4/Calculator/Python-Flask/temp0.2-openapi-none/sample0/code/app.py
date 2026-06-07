import ast
import operator
import os

from flask import Flask, jsonify, render_template_string, request


app = Flask(__name__)


ALLOWED_BINARY_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

ALLOWED_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def safe_eval(expression: str):
    if not isinstance(expression, str):
        raise ValueError("Expression must be a string")

    expression = expression.strip()
    if not expression:
        raise ValueError("Expression cannot be empty")

    try:
        parsed = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("Invalid expression") from exc

    def eval_node(node):
        if isinstance(node, ast.Expression):
            return eval_node(node.body)

        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError("Only numeric constants are allowed")

        if isinstance(node, ast.Num):  # pragma: no cover
            return node.n

        if isinstance(node, ast.BinOp):
            op_type = type(node.op)
            if op_type not in ALLOWED_BINARY_OPS:
                raise ValueError("Operator not allowed")
            left = eval_node(node.left)
            right = eval_node(node.right)
            try:
                return ALLOWED_BINARY_OPS[op_type](left, right)
            except ZeroDivisionError as exc:
                raise ValueError("Division by zero") from exc

        if isinstance(node, ast.UnaryOp):
            op_type = type(node.op)
            if op_type not in ALLOWED_UNARY_OPS:
                raise ValueError("Operator not allowed")
            return ALLOWED_UNARY_OPS[op_type](eval_node(node.operand))

        raise ValueError("Invalid expression")

    result = eval_node(parsed)

    if isinstance(result, float) and result.is_integer():
        return str(int(result))
    return str(result)


@app.get("/")
def index():
    return render_template_string(
        """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1.0" />
            <title>MyCalculator</title>
            <style>
                body {
                    font-family: Arial, sans-serif;
                    max-width: 700px;
                    margin: 40px auto;
                    padding: 0 16px;
                    background: #f7f7f7;
                    color: #222;
                }
                .card {
                    background: white;
                    border-radius: 12px;
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
                pre {
                    background: #f0f0f0;
                    padding: 12px;
                    border-radius: 8px;
                    overflow-x: auto;
                }
                .hint {
                    color: #555;
                    font-size: 14px;
                }
            </style>
        </head>
        <body>
            <div class="card">
                <h1>MyCalculator</h1>
                <p>Enter an arithmetic expression to calculate.</p>
                <input id="expression" type="text" value="1 + 2*3" placeholder="e.g. 1 + 2*3" />
                <button onclick="calculate()">Calculate</button>
                <p class="hint">Supported operators: +, -, *, /, //, %, **, parentheses, unary + and -</p>
                <h3>Result</h3>
                <pre id="output">Waiting for input...</pre>
            </div>

            <script>
                async function calculate() {
                    const expression = document.getElementById('expression').value;
                    const output = document.getElementById('output');

                    output.textContent = 'Calculating...';

                    try {
                        const response = await fetch('/calculator', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json'
                            },
                            body: JSON.stringify({ expression })
                        });

                        const data = await response.json().catch(() => ({}));

                        if (!response.ok) {
                            output.textContent = JSON.stringify(data, null, 2) || 'Invalid input';
                            return;
                        }

                        output.textContent = JSON.stringify(data, null, 2);
                    } catch (error) {
                        output.textContent = JSON.stringify({ error: 'Request failed' }, null, 2);
                    }
                }
            </script>
        </body>
        </html>
        """
    )


@app.post("/calculator")
def calculator():
    if not request.is_json:
        return jsonify({"error": "Request body must be application/json"}), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON body"}), 400

    expression = data.get("expression")
    if expression is None:
        return jsonify({"error": "Missing required field: expression"}), 400

    try:
        result = safe_eval(expression)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({"result": result}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port)