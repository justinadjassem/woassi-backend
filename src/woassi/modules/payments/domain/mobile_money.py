"""Togolese Mobile Money numbers, and the KPrimePay network each one belongs to.

https://developers.kprimepay.com/index?v=v2#ussd: numbers are sent in local form (8 digits);
70-73 and 90-93 are Mixx by Yas (Togocom), every other Togolese prefix is Moov Money (Flooz).
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum

# +228, then a mobile number: Togolese mobiles start with 7 or 9.
_TOGO_MOBILE = re.compile(r"\+228([79]\d{7})")
_MIXX_PREFIXES = frozenset({"70", "71", "72", "73", "90", "91", "92", "93"})


class Operator(StrEnum):
    """KPrimePay's gateway codes for USSD push."""

    MIXX_YAS_TG = "MIXX-YAS-TG"
    MOOV_MONEY_TG = "MOOV-MONEY-TG"


@dataclass(frozen=True, slots=True)
class MobileMoneyNumber:
    """A number that can approve a USSD push. Personal data: kept out of every repr."""

    e164: str = field(repr=False)
    operator: Operator

    @property
    def local(self) -> str:
        """The 8 digits KPrimePay expects, without +228."""
        return self.e164[4:]

    @property
    def masked(self) -> str:
        """`90 •• •• 56`: enough for the customer to recognise it, not to spread it."""
        return f"{self.local[:2]} •• •• {self.local[-2:]}"


def togolese_mobile(e164: str | None) -> MobileMoneyNumber | None:
    """The number if it is a Togolese mobile in E.164, else None (foreign, landline, missing)."""
    if not e164:
        return None
    phone = e164 if e164.startswith("+") else f"+{e164}"
    match = _TOGO_MOBILE.fullmatch(phone)
    if match is None:
        return None
    local = match.group(1)
    operator = Operator.MIXX_YAS_TG if local[:2] in _MIXX_PREFIXES else Operator.MOOV_MONEY_TG
    return MobileMoneyNumber(e164=phone, operator=operator)
