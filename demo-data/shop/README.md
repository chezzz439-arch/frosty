# Acme Shop — checkout

A small checkout service. Every pull request is reviewed by **PR Guardian**: it judges the change
against our rules, and if it finds a real violation it fixes the code, runs the test suite, and merges
only when the tests pass. When it isn't sure, it hands the PR to a human.

```
checkout/   cards.py (Luhn, mask_pan), payments.py (charge), gateway.py (processor client)
tests/      pytest suite, including the PCI log-safety tests
```

Run the tests: `pip install -r requirements-dev.txt && python -m pytest`
