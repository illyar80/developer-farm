const express = require('express');
const router = express.Router();
const { validationResult } = require('express-validator');

// POST /api/users endpoint
router.post('/', [
  // Email validation
  check('email', 'Invalid email format').isEmail(),
  
  // Name length validation
  check('name', 'Name must be between 2 and 50 characters')
    .isLength({ min: 2, max: 50 })
], (req, res) => {
  const errors = validationResult(req);
  if (!errors.isEmpty()) {
    return res.status(400).json({ errors: errors.array() });
  }

  // Proceed with user creation logic here
  const { email, name } = req.body;
  console.log(`Creating user with email: ${email} and name: ${name}`);

  // Example response for successful creation
  res.status(201).json({ message: 'User created successfully' });
});

module.exports = router;