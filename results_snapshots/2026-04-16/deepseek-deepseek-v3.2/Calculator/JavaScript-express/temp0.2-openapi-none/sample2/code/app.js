const express = require('express');
const app = express();
const PORT = process.env.PORT || 5001;

// Middleware
app.use(express.json());

// Helper function to evaluate arithmetic expression safely
function evaluateExpression(expression) {
    // Remove any whitespace
    const cleanExpr = expression.replace(/\s+/g, '');
    
    // Validate expression contains only allowed characters
    if (!/^[\d+\-*/.()]+$/.test(cleanExpr)) {
        throw new Error('Invalid characters in expression');
    }
    
    // Validate expression structure
    if (!/^[\d()]+([+\-*/][\d()]+)*$/.test(cleanExpr)) {
        throw new Error('Invalid expression structure');
    }
    
    // Use Function constructor to evaluate the expression
    // This is safer than eval but still requires validation above
    try {
        // Create a function that returns the result
        const func = new Function(`return ${cleanExpr}`);
        const result = func();
        
        // Check if result is a valid number
        if (typeof result !== 'number' || !isFinite(result)) {
            throw new Error('Invalid calculation result');
        }
        
        return result.toString();
    } catch (error) {
        throw new Error('Failed to evaluate expression');
    }
}

// POST /calculator endpoint
app.post('/calculator', (req, res) => {
    try {
        const { expression } = req.body;
        
        // Validate request body
        if (!expression || typeof expression !== 'string') {
            return res.status(400).json({ error: 'Invalid input' });
        }
        
        // Evaluate the expression
        const result = evaluateExpression(expression);
        
        // Return the result
        return res.status(200).json({ result });
        
    } catch (error) {
        return res.status(400).json({ error: 'Invalid input' });
    }
});

// Health check endpoint
app.get('/health', (req, res) => {
    res.status(200).json({ status: 'ok' });
});

// Start server
app.listen(PORT, '0.0.0.0', () => {
    console.log(`Calculator server listening on port ${PORT}`);
});

// Handle uncaught errors
process.on('uncaughtException', (err) => {
    console.error('Uncaught Exception:', err);
});

process.on('unhandledRejection', (err) => {
    console.error('Unhandled Rejection:', err);
});