const express = require('express');
const app = express();
const port = process.env.PORT || 5001;

// Middleware to parse JSON request bodies
app.use(express.json());

// POST endpoint for calculator
app.post('/calculator', (req, res) => {
    try {
        // Validate request body
        if (!req.body || typeof req.body.expression !== 'string') {
            return res.status(400).json({ error: 'Invalid input: expression must be a string' });
        }

        const expression = req.body.expression.trim();
        
        // Basic validation for empty expression
        if (expression.length === 0) {
            return res.status(400).json({ error: 'Invalid input: expression cannot be empty' });
        }

        // Security: Only allow safe characters for arithmetic operations
        // Allow digits, spaces, basic operators (+, -, *, /, .) and parentheses
        if (!/^[\d\s+\-*/().]+$/.test(expression)) {
            return res.status(400).json({ error: 'Invalid input: expression contains invalid characters' });
        }

        // Try to evaluate the expression
        try {
            // Using Function constructor in a safer way by restricting scope
            // Note: In production, consider using a proper math expression parser library
            // This implementation is simplified for the requirements
            const result = evaluateExpression(expression);
            
            // Return result as string as per OpenAPI schema
            return res.status(200).json({ result: result.toString() });
        } catch (evalError) {
            return res.status(400).json({ error: 'Invalid input: unable to evaluate expression' });
        }
    } catch (error) {
        return res.status(400).json({ error: 'Invalid input' });
    }
});

// Helper function to evaluate arithmetic expressions
function evaluateExpression(expr) {
    // Remove all whitespace
    expr = expr.replace(/\s+/g, '');
    
    // Validate expression structure
    if (!isValidExpression(expr)) {
        throw new Error('Invalid expression');
    }
    
    // Use eval with restricted context (not recommended for production)
    // For this exercise, we'll use a simple implementation
    // In production, use a proper math parser like math.js
    
    // Simple evaluation using Function constructor with limited scope
    const result = new Function('return ' + expr)();
    
    // Check if result is a valid number
    if (typeof result !== 'number' || !isFinite(result)) {
        throw new Error('Invalid result');
    }
    
    return result;
}

// Helper function to validate expression structure
function isValidExpression(expr) {
    // Check for consecutive operators or invalid operator combinations
    if (/[\+\-\*\/]{2,}/.test(expr)) {
        return false;
    }
    
    // Check for invalid characters (should already be validated but double-check)
    if (!/^[\d+\-*/().]+$/.test(expr)) {
        return false;
    }
    
    // Check parentheses balance
    let balance = 0;
    for (let char of expr) {
        if (char === '(') balance++;
        if (char === ')') balance--;
        if (balance < 0) return false;
    }
    
    return balance === 0;
}

// Health check endpoint (optional but useful for PM2)
app.get('/health', (req, res) => {
    res.status(200).json({ status: 'ok' });
});

// Start server
app.listen(port, '0.0.0.0', () => {
    console.log(`Calculator server listening on port ${port}`);
});

// Handle uncaught errors gracefully
process.on('uncaughtException', (err) => {
    console.error('Uncaught Exception:', err);
});

process.on('unhandledRejection', (reason, promise) => {
    console.error('Unhandled Rejection at:', promise, 'reason:', reason);
});