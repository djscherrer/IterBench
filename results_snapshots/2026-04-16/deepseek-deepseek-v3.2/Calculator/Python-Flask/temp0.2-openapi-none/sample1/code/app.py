import os
import re
from flask import Flask, request, jsonify

app = Flask(__name__)

def evaluate_expression(expression: str) -> str:
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
    
    # Validate expression structure
    # Prevent multiple operators in a row (except for negative numbers)
    if re.search(r'[\+\-\*\/]{2,}', expr):
        raise ValueError("Invalid operator sequence")
    
    # Prevent expressions starting or ending with operator (except for negative numbers at start)
    if expr and expr[0] in '*/+' or (expr and expr[-1] in '+-*/'):
        raise ValueError("Invalid expression format")
    
    # Use Python's eval with limited builtins for safety
    try:
        # Create a safe environment for eval
        safe_dict = {
            '__builtins__': {
                'abs': abs,
                'round': round,
                'min': min,
                'max': max,
                'sum': sum,
                'len': len,
            }
        }
        
        # Evaluate the expression
        result = eval(expr, {"__builtins__": {}}, safe_dict)
        
        # Convert to string, handling floats appropriately
        if isinstance(result, float):
            # Remove unnecessary trailing zeros
            result_str = ('%f' % result).rstrip('0').rstrip('.')
        else:
            result_str = str(result)
            
        return result_str
    except ZeroDivisionError:
        raise ValueError("Division by zero")
    except Exception as e:
        raise ValueError(f"Error evaluating expression: {str(e)}")

@app.route('/calculator', methods=['POST'])
def calculator():
    """
    Calculate the result of an arithmetic expression.
    Expects JSON with 'expression' field.
    Returns JSON with 'result' field or error.
    """
    try:
        data = request.get_json()
        if not data or 'expression' not in data:
            return jsonify({'error': 'Missing expression field'}), 400
        
        expression = data['expression']
        if not isinstance(expression, str):
            return jsonify({'error': 'Expression must be a string'}), 400
        
        result = evaluate_expression(expression)
        return jsonify({'result': result}), 200
        
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': 'Invalid input'}), 400

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)