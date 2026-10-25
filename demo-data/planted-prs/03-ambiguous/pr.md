branch: fix/checkout-timeout
title: Stop checkout hanging when the processor is slow
expect: escalate
---
When the processor is slow, customers stare at a spinner for up to 30 seconds and then get an error,
even though most of those charges go through. This drops the timeout to 3s and returns "pending"
instead of failing, so checkout shows "processing" and the nightly reconciliation job settles it.

Updated the gateway tests for the new behavior.
