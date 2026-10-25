# Rules for the Acme Shop checkout repo

You are judging a pull request against these rules. Each rule says which decision it leads to.
Cite the rule number in your reason (for example "R2: published test card number in a test file").

**Where things live:** `checkout/` is application code that runs in production and handles real
customer cards. `tests/` is the pytest suite; it never runs in production and never sees real cards.

## R1 — Card data must never reach logs, output or error messages → `fix`

Application code (anything outside `tests/`) must not write a full card number (PAN), a CVV, or a
full bank account number to a logger, `print`, an exception message, or any other output.

This is a real violation even when the PR says it's for debugging or support.

**How to fix it (mechanical, no human needed):** keep the log line and its purpose, but pass the number
through `mask_pan()` from `checkout/cards.py`, which keeps only the last 4 digits
(`************1111`). Remove CVVs from output entirely. Change only the offending lines: don't delete
the log line, don't edit tests, and don't touch other files. The fix must pass the whole test suite,
including any tests the PR adds.

## R2 — Published test card numbers in test files → `allow`

Card networks publish test numbers that can't be charged, such as `4111111111111111`,
`4012888888881881`, `4242424242424242`, `5555555555554444`, `378282246310005` and
`6011111111111117`. Inside `tests/` they are test data, not leaks: allow them. A number that fails the
Luhn check (for example one digit changed, to test rejection) isn't a card number at all.

The same digits in application code that logs or outputs them are covered by R1. Judge by the file
and what the code does with the number, not by the digits.

## R3 — Payment failure handling → `escalate`

Changes to how a charge behaves when the processor is slow or failing need the payments owner. This
covers timeouts, retries, catching or swallowing exceptions, and returning a status like "pending"
instead of raising, in `checkout/gateway.py` or `checkout/payments.py`. A wrong call here can ship
unpaid orders or double-charge customers, and whether it's right depends on processor behavior the
diff doesn't show. Escalate even if the change looks reasonable. Don't try to fix it.

## R4 — Weakening the tests → `escalate`

If a PR deletes a test, loosens an assertion, or rewrites a test so that it expects the new behavior,
especially anything in `tests/test_log_safety.py`, a human must check that the old behavior was really
wrong. Adding new tests is fine.

## R5 — Secrets and transport security → `escalate`

Committing a credential (API key, password, private key, token) or disabling TLS or certificate
verification needs a human, because it may already be leaked.

## Anything else

Ordinary changes that touch none of the above: `allow`, with reason "no rule applies".
