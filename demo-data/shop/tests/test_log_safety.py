"""PCI guard: card numbers must never reach the logs. Do not weaken this test."""
import logging
import re

import pytest

from checkout.models import Card
from checkout.payments import PaymentError, charge

FULL_PAN = re.compile(r"\d{13,19}")


def test_successful_charge_logs_no_card_number(caplog, fake_gateway, visa_card, order):
    with caplog.at_level(logging.DEBUG):
        charge(order, visa_card, fake_gateway)
    assert visa_card.number not in caplog.text
    assert not FULL_PAN.search(caplog.text), f"full card number in logs: {caplog.text!r}"


def test_rejected_card_error_has_no_card_number(fake_gateway, order):
    bad = Card(number="4012888888881882", exp="12/30", cvv="123")
    with pytest.raises(PaymentError) as err:
        charge(order, bad, fake_gateway)
    assert not FULL_PAN.search(str(err.value))
