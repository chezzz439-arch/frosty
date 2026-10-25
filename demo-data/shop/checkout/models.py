from dataclasses import dataclass


@dataclass(frozen=True)
class Card:
    number: str
    exp: str
    cvv: str


@dataclass(frozen=True)
class Order:
    id: str
    total: float
    currency: str = "USD"
