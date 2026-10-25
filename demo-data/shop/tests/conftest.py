import pytest

from checkout.models import Card, Order


class FakeGateway:
    def __init__(self):
        self.last_charge = None

    def charge(self, number, amount, currency="USD"):
        self.last_charge = {"number": number, "amount": amount, "currency": currency}
        return {"status": "succeeded", "id": "ch_test_1"}


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {"status": "succeeded"}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return self.response


@pytest.fixture
def fake_gateway():
    return FakeGateway()


@pytest.fixture
def visa_card():
    # Visa's published test number, not a real card.
    return Card(number="4012888888881881", exp="12/30", cvv="123")


@pytest.fixture
def order():
    return Order(id="A100", total=42.50)
