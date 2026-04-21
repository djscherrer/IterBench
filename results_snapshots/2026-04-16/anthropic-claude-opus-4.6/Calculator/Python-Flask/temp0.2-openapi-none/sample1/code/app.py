import os
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/calculator', methods=['POST'])
def calculator():
    data = request.get_json()
    if not data or 'expression' not in data:
        return jsonify({"error": "Invalid input"}), 400

    expression = data['expression']

    # Validate the expression: only allow digits, operators, parentheses, spaces, and decimal points
    allowed_chars = set('0123456789+-*/().% ')
    if not all(c in allowed_chars for c in expression):
        return jsonify({"error": "Invalid input"}), 400

    try:
        # Use eval with restricted builtins for safety
        result = eval(expression, {"__builtins__": {}}, {})
    except Exception:
        return jsonify({"error": "Invalid input"}), 400

    # Format result: if it's a float that is a whole number, display as int
    if isinstance(result, float) and result == int(result):
        result = int(result)

    return jsonify({"result": str(result)}), 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)