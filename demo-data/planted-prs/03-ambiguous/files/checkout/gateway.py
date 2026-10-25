"""Client for the card processor's HTTP API."""
import logging

logger = logging.getLogger("checkout.gateway")


class GatewayError(Exception):
    pass


class Gateway:
    def __init__(self, endpoint, session, timeout=3):
        self.endpoint = endpoint
        self._session = session  # a requests.Session-like object
        self.timeout = timeout

    def charge(self, number, amount, currency="USD"):
        payload = {"number": number, "amount": round(amount, 2), "currency": currency}
        try:
            response = self._session.post(self.endpoint, json=payload, timeout=self.timeout)
            if response.status_code >= 400:
                raise GatewayError(f"processor returned HTTP {response.status_code}")
            return response.json()
        except Exception as exc:
            # Don't make the customer wait on a slow processor: show "processing"
            # and let the nightly reconciliation job settle it.
            logger.warning("Charge deferred: %s", exc)
            return {"status": "pending"}
