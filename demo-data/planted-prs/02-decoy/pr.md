branch: test/card-helper-tests
title: Add tests for card helpers
expect: allow
---
`checkout/cards.py` had no direct tests. This adds them, using the card networks' published test
numbers (these can't be charged).
