import logging

from .cards import luhn_valid, mask_pan

logger = logging.getLogger("checkout.payments")


class PaymentError(Exception):
    pass


def charge(order, card, gateway):
    """Charge a card for an order through the payment gateway."""
    if not luhn_valid(card.number):
        raise PaymentError(f"Card {mask_pan(card.number)} failed validation")

    logger.info("Charging order %s for %.2f %s", order.id, order.total, order.currency)
    logger.info("Card used: %s", card.number)
    response = gateway.charge(
        number=card.number,
        amount=order.total,
        currency=order.currency,
    )
    logger.info("Order %s charge status: %s", order.id, response.get("status"))
    return response
