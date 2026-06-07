import os
import ast
import operator
from flask import Flask, request, jsonify

app = Flask(__name__)

# Safe evaluation of arithmetic expressions
ALLOWED_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

def safe_eval(node):
    if isinstance(node, ast.Expression):
        return safe_eval(node.body)
    elif isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("Invalid constant")
    elif isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in ALLOWED_OPERATORS:
            raise ValueError(f"Unsupported operator: {op_type.__name__}")
        left = safe_eval(node.left)
        right = safe_eval(node.right)
        return ALLOWED_OPERATORS[op_type](left, right)
    elif isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in ALLOWED_OPERATORS:
            raise ValueError(f"Unsupported unary operator: {op_type.__name__}")
        operand = safe_eval(node.operand)
        return ALLOWED_OPERATORS[op_type](operand)
    else:
        raise ValueError(f"Unsupported expression type: {type(node).__name__}")


def evaluate_expression(expression):
    tree = ast.parse(expression, mode='eval')
    return safe_eval(tree)


@app.route('/calculator', methods=['POST'])
def calculator():
    data = request.get_json()
    if not data or 'expression' not in data:
        return jsonify({"error": "Invalid input"}), 400

    expression = data['expression']
    if not isinstance(expression, str):
        return jsonify({"error": "Invalid input"}), 400

    try:
        result = evaluate_expression(expression)
        # Format result: if it's an integer value, show without decimal
        if isinstance(result, float) and result == int(result):
            result_str = str(int(result))
        else:
            result_str = str(result)
        return jsonify({"result": result_str}), 200
    except Exception:
        return jsonify({"error": "Invalid input"}), 400


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)