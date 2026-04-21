const express = require('express');

const app = express();
app.use(express.json());

app.post('/calculator', (req, res) => {
  const { expression } = req.body;

  if (!expression || typeof expression !== 'string') {
    return res.status(400).json({ error: 'Invalid input' });
  }

  try {
    // Validate that the expression only contains safe characters:
    // digits, whitespace, decimal points, and arithmetic operators (+, -, *, /, parentheses)
    const sanitized = expression.trim();
    if (!/^[\d\s+\-*/().]+$/.test(sanitized)) {
      return res.status(400).json({ error: 'Invalid input' });
    }

    // Evaluate the expression safely using Function constructor
    // (we've already validated the input contains only math characters)
    const result = Function(`"use strict"; return (${sanitized})`)();

    if (typeof result !== 'number' || !isFinite(result)) {
      return res.status(400).json({ error: 'Invalid input' });
    }

    return res.status(200).json({ result: String(result) });
  } catch (e) {
    return res.status(400).json({ error: 'Invalid input' });
  }
});

const PORT = process.env.PORT || 5001;
app.listen(PORT, '0.0.0.0', () => {
  console.log(`Server running on port ${PORT}`);
});