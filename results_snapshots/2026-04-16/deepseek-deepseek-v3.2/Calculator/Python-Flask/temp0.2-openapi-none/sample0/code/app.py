import os
import re
from flask import Flask, request, jsonify

app = Flask(__name__)

def safe_eval_expression(expr: str) -> str:
    """
    Safely evaluate a basic arithmetic expression containing numbers, +, -, *, /, and parentheses.
    Only allows digits, spaces, and the operators + - * / ( ).
    """
    # Remove all whitespace
    expr = expr.replace(" ", "")
    
    # Validate characters
    if not re.fullmatch(r'[\d\+\-\*\/\(\)]+', expr):
        raise ValueError("Invalid characters in expression")
    
    # Validate structure: no consecutive operators, proper parentheses
    if re.search(r'[\+\-\*\/]{2,}', expr):
        raise ValueError("Consecutive operators not allowed")
    
    # Check for division by zero
    if '/0' in expr:
        raise ValueError("Division by zero")
    
    # Use Python's eval with limited builtins for safety
    # Only allow basic arithmetic operations
    allowed_names = {}
    code = compile(expr, "<string>", "eval")
    for name in code.co_names:
        if name not in allowed_names:
            raise ValueError(f"Use of disallowed name: {name}")
    
    result = eval(expr, {"__builtins__": {}}, allowed_names)
    return str(result)

@app.route('/calculator', methods=['POST'])
def calculate():
    """
    Calculate the result of an arithmetic expression.
    Expects JSON with an 'expression' field.
    Returns JSON with a 'result' field or error.
    """
    try:
        data = request.get_json()
        if not data or 'expression' not in data:
            return jsonify({"error": "Missing 'expression' field"}), 400
        
        expression = data['expression']
        if not isinstance(expression, str):
            return jsonify({"error": "Expression must be a string"}), 400
        
        result = safe_eval_expression(expression)
        return jsonify({"result": result}), 200
    
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": "Invalid expression"}), 400

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)