"""Value objects shared by every module."""

import re
from dataclasses import dataclass

from woassi.shared.domain.errors import InvalidData

E164 = re.compile(r"\+[1-9]\d{6,14}")


@dataclass(frozen=True, slots=True, order=True)
class Amount:
    """Amount in CFA francs (XOF): a non-negative integer, no cents."""

    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise InvalidData("An amount must be an integer.", code="amount.invalid")
        if self.value < 0:
            raise InvalidData("An amount cannot be negative.", code="amount.negative")

    def __add__(self, other: "Amount") -> "Amount":
        """Sum of two amounts."""
        return Amount(self.value + other.value)

    def __mul__(self, quantity: int) -> "Amount":
        """Amount for `quantity` units, e.g. a line total."""
        return Amount(self.value * quantity)

    def __str__(self) -> str:
        """Display form with a space as thousands separator: `12 500 FCFA`."""
        return f"{self.value:,} FCFA".replace(",", " ")


@dataclass(frozen=True, slots=True)
class PhoneNumber:
    """Phone number in E.164 format. Parsing free-form input is PhoneNormalizer's job."""

    value: str

    def __post_init__(self) -> None:
        if not E164.fullmatch(self.value):
            raise InvalidData("A phone number must be in E.164 format.", code="phone.invalid")

    def __str__(self) -> str:
        return self.value
