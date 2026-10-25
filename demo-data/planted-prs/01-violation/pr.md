branch: feature/log-card-used
title: Log which card was used on each charge
expect: fix
---
Support keeps asking which card a customer was charged on when they call about a payment.
This adds a log line with the card used for each charge, plus a test for it.

- `checkout/payments.py`: log the card on every charge
- `tests/test_payments.py`: new test that the card shows up in the logs
