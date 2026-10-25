import pytest

from checkout.gateway import Gateway, GatewayError

from conftest import FakeResponse, FakeSession


def test_gateway_posts_rounded_amount():
    session = FakeSession()
    Gateway("https://processor.test/charge", session).charge("4012888888881881", 19.999)
    assert session.calls[0]["json"]["amount"] == 20.0
    assert session.calls[0]["timeout"] == 30


def test_gateway_raises_on_http_error():
    session = FakeSession(FakeResponse(status_code=502))
    with pytest.raises(GatewayError):
        Gateway("https://processor.test/charge", session).charge("4012888888881881", 5.00)
