import os
import re
from flask import Flask, request, jsonify

app = Flask(__name__)

def safe_eval(expression):
    """
    Safely evaluate a basic arithmetic expression.
    Only allows numbers, spaces, and operators + - * / ( )
    """
    # Remove all whitespace
    expr = expression.replace(" ", "")
    
    # Validate characters: only digits, operators, and parentheses allowed
    if not re.match(r'^[\d\+\-\*\/\(\)\.]+$', expr):
        raise ValueError("Invalid characters in expression")
    
    # Validate parentheses
    stack = []
    for char in expr:
        if char == '(':
            stack.append(char)
        elif char == ')':
            if not stack:
                raise ValueError("Mismatched parentheses")
            stack.pop()
    if stack:
        raise ValueError("Mismatched parentheses")
    
    # Validate operator usage
    if re.search(r'[\+\-\*\/]{2,}', expr):
        raise ValueError("Consecutive operators")
    
    if expr.startswith(('+', '*', '/', '.')) or expr.endswith(('+', '-', '*', '/', '.')):
        raise ValueError("Invalid operator placement")
    
    # Use Python's eval with limited builtins for safety
    allowed_names = {}
    try:
        result = eval(expr, {"__builtins__": {}}, allowed_names)
    except ZeroDivisionError:
        raise ValueError("Division by zero")
    except Exception:
        raise ValueError("Invalid expression syntax")
    
    # Return result as string
    return str(result)

@app.route('/calculator', methods=['POST'])
def calculate():
    """
    Calculate the result of an arithmetic expression.
    Expects JSON with 'expression' field.
    Returns JSON with 'result' field or error.
    """
    try:
        data = request.get_json()
        if not data or 'expression' not in data:
            return jsonify({"error": "Missing expression"}), 400
        
        expression = data['expression']
        if not isinstance(expression, str):
            return jsonify({"error": "Expression must be a string"}), 400
        
        result = safe_eval(expression)
        return jsonify({"result": result}), 200
    
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Invalid input"}), 400

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for monitoring."""
    return jsonify({"status": "healthy"}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)