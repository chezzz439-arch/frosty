"""Seed the local database with the three planted PRs, so the judge can be
exercised before /trigger and /demo-data land.

    python engine/seed_fake_rows.py

These diffs stand in for Erik's real planted PRs. They exist to check that the
judge rules the way CLAUDE.md requires: fix #101, ALLOW #102, escalate #103.
The decoy (#102) carries the same card number as the violation (#101) on
purpose -- that pair is the whole argument of the demo.
"""

from __future__ import annotations

import db

# PR #101 -- the violation. App code logging a card number. Must be fixed.
VIOLATION = """\
diff --git a/checkout/payments.py b/checkout/payments.py
--- a/checkout/payments.py
+++ b/checkout/payments.py
@@ -14,6 +14,7 @@ def charge(order, card):
     logger.info("Charging order %s for %.2f", order.id, order.total)
+    logger.info("Card used: %s", card.number)
     response = gateway.charge(
         number=card.number,
         amount=order.total,
"""

# PR #102 -- the decoy. Same digits, but it's test data in a test file.
# Must be ALLOWED. Catching this fails the demo.
DECOY = """\
diff --git a/tests/test_payments.py b/tests/test_payments.py
--- a/tests/test_payments.py
+++ b/tests/test_payments.py
@@ -1,8 +1,16 @@
 import pytest
 from checkout.payments import charge

+# Visa's published test number -- not a real card.
+TEST_CARD = "4111111111111111"
+
+
+def test_charge_sends_card_to_gateway(fake_gateway):
+    card = Card(number=TEST_CARD, exp="12/30", cvv="123")
+    charge(order=Order(id="A1", total=9.99), card=card)
+    assert fake_gateway.last_charge["number"] == TEST_CARD
"""

# PR #103 -- ambiguous. Lowers a timeout and widens an exception catch. Not
# obviously a rule violation, not obviously safe. Must escalate.
AMBIGUOUS = """\
diff --git a/checkout/gateway.py b/checkout/gateway.py
--- a/checkout/gateway.py
+++ b/checkout/gateway.py
@@ -22,9 +22,9 @@ class Gateway:
     def charge(self, number, amount, currency="USD"):
-        response = self._session.post(
-            self.endpoint, json=payload, timeout=30
-        )
-        response.raise_for_status()
-        return response.json()
+        try:
+            response = self._session.post(
+                self.endpoint, json=payload, timeout=2
+            )
+            return response.json()
+        except Exception:
+            return {"status": "pending"}
"""

PLANTED = [
    (101, "violation", ["checkout/payments.py"], VIOLATION),
    (102, "decoy", ["tests/test_payments.py"], DECOY),
    (103, "ambiguous", ["checkout/gateway.py"], AMBIGUOUS),
]

# One past receipt, so the memory lookup has something to return.
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


def main() -> int:
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
    print("Now: python engine/run.py 101")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
