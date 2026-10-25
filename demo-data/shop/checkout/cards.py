"""Card number helpers. Use mask_pan() whenever a card number has to be shown or logged."""


def _digits(number: str) -> str:
    return "".join(ch for ch in str(number) if ch.isdigit())


def luhn_valid(number: str) -> bool:
    digits = _digits(number)
    if not 12 <= len(digits) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def mask_pan(number: str) -> str:
    """'4111111111111111' -> '************1111'. Safe to log."""
    digits = _digits(number)
    return "*" * max(len(digits) - 4, 0) + digits[-4:]


def brand(number: str) -> str:
    digits = _digits(number)
    if digits.startswith("4"):
        return "visa"
    if digits[:2] in {"51", "52", "53", "54", "55"} or "2221" <= digits[:4] <= "2720":
        return "mastercard"
    if digits[:2] in {"34", "37"}:
        return "amex"
    if digits.startswith("6011") or digits.startswith("65"):
        return "discover"
    return "unknown"
