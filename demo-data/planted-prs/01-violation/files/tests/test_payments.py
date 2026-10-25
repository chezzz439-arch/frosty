import logging

import pytest

from checkout.models import Card
from checkout.payments import PaymentError, charge


def test_charge_sends_card_to_gateway(fake_gateway, visa_card, order):
    result = charge(order, visa_card, fake_gateway)
    assert result["status"] == "succeeded"
    assert fake_gateway.last_charge == {"number": visa_card.number, "amount": 42.50, "currency": "USD"}


def test_invalid_card_is_rejected_before_gateway(fake_gateway, order):
    bad = Card(number="4012888888881882", exp="12/30", cvv="123")
    with pytest.raises(PaymentError):
        charge(order, bad, fake_gateway)
    assert fake_gateway.last_charge is None


def test_charge_logs_which_card_was_used(caplog, fake_gateway, order):
    card = Card(number="4111111111111111", exp="12/30", cvv="123")
    with caplog.at_level(logging.INFO):
        charge(order, card, fake_gateway)
    assert "Card used" in caplog.text
    assert "1111" in caplog.text
