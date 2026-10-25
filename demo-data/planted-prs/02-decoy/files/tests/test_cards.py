"""Tests for checkout/cards.py.

Every card number here is a card network's published TEST number. None of them can be charged.
"""
from checkout.cards import brand, luhn_valid, mask_pan

VISA_TEST = "4111111111111111"
MASTERCARD_TEST = "5555555555554444"
AMEX_TEST = "378282246310005"
DISCOVER_TEST = "6011111111111117"


def test_published_test_numbers_pass_luhn():
    for number in (VISA_TEST, MASTERCARD_TEST, AMEX_TEST, DISCOVER_TEST):
        assert luhn_valid(number), number


def test_one_digit_off_fails_luhn():
    assert not luhn_valid("4111111111111112")


def test_mask_keeps_only_last_four():
    assert mask_pan(VISA_TEST) == "************1111"
    assert mask_pan(AMEX_TEST) == "***********0005"


def test_brand_detection():
    assert brand(VISA_TEST) == "visa"
    assert brand(MASTERCARD_TEST) == "mastercard"
    assert brand(AMEX_TEST) == "amex"
    assert brand(DISCOVER_TEST) == "discover"
