const express = require('express');
const app = express();
const port = process.env.PORT || 5001;

// Middleware to parse JSON bodies
app.use(express.json());

// Health check endpoint (optional but useful)
app.get('/health', (req, res) => {
    res.status(200).json({ status: 'ok' });
});

// POST /calculator endpoint
app.post('/calculator', (req, res) => {
    try {
        const { expression } = req.body;
        
        // Validate input
        if (expression === undefined || expression === null || typeof expression !== 'string') {
            return res.status(400).json({ error: 'Invalid input: expression must be a string' });
        }
        
        if (expression.trim() === '') {
            return res.status(400).json({ error: 'Invalid input: expression cannot be empty' });
        }
        
        // Sanitize expression: allow only numbers, basic operators, parentheses, and spaces
        const sanitizedExpression = expression.replace(/[^0-9+\-*/().\s]/g, '');
        
        // Additional safety check to prevent malicious code execution
        // We'll use a simple eval alternative for basic arithmetic
        try {
            // Use Function constructor as a safer alternative to eval
            // This creates a function in a restricted scope
            const result = evaluateExpression(sanitizedExpression);
            
            // Return result as string as per OpenAPI schema
            return res.status(200).json({ result: result.toString() });
        } catch (error) {
            return res.status(400).json({ error: 'Invalid mathematical expression' });
        }
    } catch (error) {
        return res.status(400).json({ error: 'Invalid input' });
    }
});

// Helper function to evaluate arithmetic expressions safely
function evaluateExpression(expr) {
    // Remove all whitespace
    const cleanExpr = expr.replace(/\s+/g, '');
    
    // Parse and evaluate the expression
    let index = 0;
    
    function parseExpression() {
        let left = parseTerm();
        
        while (index < cleanExpr.length && (cleanExpr[index] === '+' || cleanExpr[index] === '-')) {
            const operator = cleanExpr[index];
            index++;
            const right = parseTerm();
            
            if (operator === '+') {
                left += right;
            } else {
                left -= right;
            }
        }
        
        return left;
    }
    
    function parseTerm() {
        let left = parseFactor();
        
        while (index < cleanExpr.length && (cleanExpr[index] === '*' || cleanExpr[index] === '/')) {
            const operator = cleanExpr[index];
            index++;
            const right = parseFactor();
            
            if (operator === '*') {
                left *= right;
            } else {
                if (right === 0) {
                    throw new Error('Division by zero');
                }
                left /= right;
            }
        }
        
        return left;
    }
    
    function parseFactor() {
        if (cleanExpr[index] === '(') {
            index++;
            const value = parseExpression();
            
            if (cleanExpr[index] !== ')') {
                throw new Error('Mismatched parentheses');
            }
            index++;
            return value;
        }
        
        // Parse number
        let start = index;
        let hasDecimal = false;
        
        while (index < cleanExpr.length && 
               (cleanExpr[index] >= '0' && cleanExpr[index] <= '9' || cleanExpr[index] === '.')) {
            if (cleanExpr[index] === '.') {
                if (hasDecimal) {
                    throw new Error('Invalid number format');
                }
                hasDecimal = true;
            }
            index++;
        }
        
        if (start === index) {
            throw new Error('Expected number');
        }
        
        const numberStr = cleanExpr.substring(start, index);
        return parseFloat(numberStr);
    }
    
    const result = parseExpression();
    
    if (index !== cleanExpr.length) {
        throw new Error('Unexpected character at end of expression');
    }
    
    return result;
}

// Error handling middleware
app.use((err, req, res, next) => {
    console.error(err.stack);
    res.status(500).json({ error: 'Internal server error' });
});

// Start server
if (require.main === module) {
    app.listen(port, '0.0.0.0', () => {
        console.log(`Calculator server listening on port ${port}`);
    });
}

module.exports = app;