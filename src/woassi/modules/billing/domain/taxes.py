"""Taxes of a billing document, and the one rounding rule.

Prices are tax-inclusive (`TTC`): what the customer pays is the catalog price, and the invoice
total is exactly the amount paid. Each line's total is `unit_price x quantity`; its amount before
tax is `total / (1 + rate)`, rounded half up to the franc, and its tax is the difference. Header
totals are sums of the line figures, so they always add up. Amounts are XOF integers, never
negative.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

BASIS_POINTS = 10_000
VAT_STANDARD_BP = 1_800  # 18 %, art. 195 CGI


class TaxCode(StrEnum):
    """How a line is taxed. Always stored, even when the tax is 0.

    VAT_18: standard VAT. NOT_SUBJECT: the seller is not registered for VAT (e.g. on the TPU) and
    must not charge it. EXEMPT: an exempt supply by a VAT-registered seller (not used yet).
    """

    VAT_18 = "VAT_18"
    NOT_SUBJECT = "NOT_SUBJECT"
    EXEMPT = "EXEMPT"


RATES_BP = {TaxCode.VAT_18: VAT_STANDARD_BP, TaxCode.NOT_SUBJECT: 0, TaxCode.EXEMPT: 0}


def tax_code_for(*, vat_registered: bool) -> TaxCode:
    """The code of every line sold by this seller."""
    return TaxCode.VAT_18 if vat_registered else TaxCode.NOT_SUBJECT


def split_inclusive(total: int, rate_bp: int) -> tuple[int, int]:
    """(amount before tax, tax) of a tax-inclusive `total`, rounded half up to the franc."""
    if total < 0 or rate_bp < 0:
        raise ValueError("Amounts and rates cannot be negative.")
    divisor = BASIS_POINTS + rate_bp
    before_tax = (2 * total * BASIS_POINTS + divisor) // (2 * divisor)
    return before_tax, total - before_tax


@dataclass(frozen=True, slots=True)
class TaxTotal:
    """Totals of the lines sharing one tax code."""

    tax_code: TaxCode
    rate_bp: int
    amount_excl_tax: int
    tax_amount: int
    total: int


class _Taxed(Protocol):
    """What `tax_totals` needs from a line."""

    @property
    def tax_code(self) -> TaxCode: ...

    @property
    def tax_rate_bp(self) -> int: ...

    @property
    def amount_excl_tax(self) -> int: ...

    @property
    def tax_amount(self) -> int: ...

    @property
    def total(self) -> int: ...


def tax_totals(lines: Iterable[_Taxed]) -> tuple[TaxTotal, ...]:
    """One total per tax code, in order of first appearance."""
    sums: dict[TaxCode, list[int]] = {}
    rates: dict[TaxCode, int] = {}
    for line in lines:
        current = sums.setdefault(line.tax_code, [0, 0, 0])
        rates[line.tax_code] = line.tax_rate_bp
        current[0] += line.amount_excl_tax
        current[1] += line.tax_amount
        current[2] += line.total
    return tuple(
        TaxTotal(code, rates[code], values[0], values[1], values[2])
        for code, values in sums.items()
    )
