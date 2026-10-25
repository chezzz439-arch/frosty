"""Seed the local database and a demo checkout, so the engine can be exercised
before /trigger and /demo-data land.

    python engine/seed_fake_rows.py
    PR_GUARDIAN_REPO=engine/.local/demo-repo python engine/run.py 101

Two things get written:

1. Three PR_DIFFS rows, standing in for Erik's planted PRs.
2. A small checkout under engine/.local/demo-repo holding the files those
   diffs describe, plus a test suite that passes.

The second one matters. The judge is asked for COMPLETE corrected files, so it
has to be able to read the complete current ones -- and the fix loop has to
have something to copy, rewrite and test. A diff alone isn't enough for either.

These stand in for the real planted PRs. They exist to check that the judge
rules the way CLAUDE.md requires: fix #101, ALLOW #102, escalate #103. The
decoy (#102) carries the same card number as the violation (#101) on purpose --
that pair is the whole argument of the demo.
"""

from __future__ import annotations

import os

import db

DEMO_REPO = os.path.join(os.path.dirname(__file__), ".local", "demo-repo")

# --- the demo checkout, as it stands on the PR branches ---------------------

PAYMENTS = '''\
"""Charging a card for an order."""

import logging

logger = logging.getLogger(__name__)


def charge(order, card, gateway):
    """Charge `card` for `order` through `gateway`."""
    logger.info("Charging order %s for %.2f", order.id, order.total)
    logger.info("Card used: %s", card.number)
    return gateway.charge(number=card.number, amount=order.total)
'''

GATEWAY = '''\
"""Client for the upstream payment gateway."""


class Gateway:
    """Posts charges to the gateway endpoint."""

    def __init__(self, session, endpoint):
        self._session = session
        self.endpoint = endpoint

    def charge(self, number, amount, currency="USD"):
        payload = {"number": number, "amount": amount, "currency": currency}
        try:
            response = self._session.post(self.endpoint, json=payload, timeout=2)
            return response.json()
        except Exception:
            return {"status": "pending"}
'''

TEST_PAYMENTS = '''\
import unittest

from checkout.payments import charge

# Visa's published test number -- not a real card.
TEST_CARD = "4111111111111111"


class Card:
    def __init__(self, number, exp, cvv):
        self.number = number
        self.exp = exp
        self.cvv = cvv


class Order:
    def __init__(self, id, total):
        self.id = id
        self.total = total


class FakeGateway:
    def __init__(self):
        self.last_charge = None

    def charge(self, number, amount):
        self.last_charge = {"number": number, "amount": amount}
        return {"status": "ok"}


class TestCharge(unittest.TestCase):
    def test_charge_sends_card_to_gateway(self):
        gateway = FakeGateway()
        card = Card(number=TEST_CARD, exp="12/30", cvv="123")
        result = charge(Order(id="A1", total=9.99), card, gateway)
        self.assertEqual(gateway.last_charge["number"], TEST_CARD)
        self.assertEqual(gateway.last_charge["amount"], 9.99)
        self.assertEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
'''

TEST_GATEWAY = '''\
import unittest

from checkout.gateway import Gateway

TEST_CARD = "4111111111111111"


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.last = None

    def post(self, url, json, timeout):
        self.last = {"url": url, "json": json, "timeout": timeout}
        if self._error is not None:
            raise self._error
        return self._response


class TestGateway(unittest.TestCase):
    def test_charge_returns_the_gateway_response(self):
        session = FakeSession(FakeResponse({"status": "ok"}))
        gateway = Gateway(session, "https://gateway.invalid/charge")
        self.assertEqual(gateway.charge(TEST_CARD, 9.99), {"status": "ok"})

    def test_charge_sends_the_amount_and_currency(self):
        session = FakeSession(FakeResponse({"status": "ok"}))
        Gateway(session, "https://gateway.invalid/charge").charge(TEST_CARD, 12.50)
        self.assertEqual(session.last["json"]["amount"], 12.50)
        self.assertEqual(session.last["json"]["currency"], "USD")


if __name__ == "__main__":
    unittest.main()
'''

DEMO_FILES = {
    "checkout/__init__.py": "",
    "checkout/payments.py": PAYMENTS,
    "checkout/gateway.py": GATEWAY,
    "tests/__init__.py": "",
    "tests/test_payments.py": TEST_PAYMENTS,
    "tests/test_gateway.py": TEST_GATEWAY,
}

# --- the diffs that produced that state --------------------------------------

# PR #101 -- the violation. App code logging a card number. Must be fixed.
VIOLATION = """\
diff --git a/checkout/payments.py b/checkout/payments.py
--- a/checkout/payments.py
+++ b/checkout/payments.py
@@ -8,4 +8,5 @@ def charge(order, card, gateway):
     \"\"\"Charge `card` for `order` through `gateway`.\"\"\"
     logger.info("Charging order %s for %.2f", order.id, order.total)
+    logger.info("Card used: %s", card.number)
     return gateway.charge(number=card.number, amount=order.total)
"""

# PR #102 -- the decoy. Same digits, but it's test data in a test file.
# Must be ALLOWED. Catching this fails the demo.
DECOY = """\
diff --git a/tests/test_payments.py b/tests/test_payments.py
--- a/tests/test_payments.py
+++ b/tests/test_payments.py
@@ -1,5 +1,8 @@
 import unittest

 from checkout.payments import charge

+# Visa's published test number -- not a real card.
+TEST_CARD = "4111111111111111"
+
@@ -30,0 +38,9 @@ class FakeGateway:
+class TestCharge(unittest.TestCase):
+    def test_charge_sends_card_to_gateway(self):
+        gateway = FakeGateway()
+        card = Card(number=TEST_CARD, exp="12/30", cvv="123")
+        result = charge(Order(id="A1", total=9.99), card, gateway)
+        self.assertEqual(gateway.last_charge["number"], TEST_CARD)
+        self.assertEqual(result["status"], "ok")
"""

# PR #103 -- ambiguous. Lowers a timeout and widens an exception catch. Not
# obviously a rule violation, not obviously safe. Must escalate.
AMBIGUOUS = """\
diff --git a/checkout/gateway.py b/checkout/gateway.py
--- a/checkout/gateway.py
+++ b/checkout/gateway.py
@@ -12,5 +12,8 @@ class Gateway:
     def charge(self, number, amount, currency="USD"):
         payload = {"number": number, "amount": amount, "currency": currency}
-        response = self._session.post(self.endpoint, json=payload, timeout=30)
-        response.raise_for_status()
-        return response.json()
+        try:
+            response = self._session.post(self.endpoint, json=payload, timeout=2)
+            return response.json()
+        except Exception:
+            return {"status": "pending"}
"""

PLANTED = [
    (101, "violation", ["checkout/payments.py"], VIOLATION),
    (102, "decoy", ["tests/test_payments.py"], DECOY),
    (103, "ambiguous", ["checkout/gateway.py"], AMBIGUOUS),
]

# Past receipts, so the memory lookup has something to return.
PAST_RECEIPTS = [
    (
        98,
        "allow",
        "Sample card number in docs/payments.md is documentation, not a leak.",
        True,
    ),
    (
        99,
        "fix",
        "Removed a logger.debug call that printed a CVV in checkout/refunds.py.",
        True,
    ),
]


def write_demo_repo(root: str = DEMO_REPO) -> str:
    """Write the demo checkout. Returns its absolute path."""
    root = os.path.abspath(root)
    for relative_path, content in DEMO_FILES.items():
        target = os.path.join(root, relative_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(content)
    return root


def main() -> int:
    root = write_demo_repo()
    for relative_path in DEMO_FILES:
        print(f"demo-repo {relative_path}")

    for pr_number, label, files, diff_text in PLANTED:
        db.insert_diff(
            pr_number=pr_number,
            repo="chezzz439-arch/frosty",
            branch=f"planted/{label}",
            files=files,
            diff_text=diff_text,
        )
        print(f"PR_DIFFS  #{pr_number}  {label}  {files[0]}")

    for pr_number, decision, reason, merged in PAST_RECEIPTS:
        db.insert_receipt(
            pr_number=pr_number,
            decision=decision,
            reason=reason,
            merged=merged,
            receipt_url=f"https://github.com/chezzz439-arch/frosty/pull/{pr_number}",
        )
        print(f"RECEIPTS  #{pr_number}  {decision}")

    print(f"\nwrote to {db.db_path()}")
    print(f"demo checkout at {root}")
    print(f"\nNow: PR_GUARDIAN_REPO={root} python engine/run.py 101")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
