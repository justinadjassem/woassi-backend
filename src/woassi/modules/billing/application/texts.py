"""Words of the billing module, in each supported language: PDF labels, WhatsApp messages and
the `woassi_invoice_ready` template. Template parameters must be a single line.

Legal mentions follow the paper standardised invoice (art. 64 LPF): a seller below the VAT
threshold (e.g. on the TPU) prints "NE FACTURE PAS LA TVA". It stays in French in every
language, as it is the wording of the tax administration.
"""

from dataclasses import dataclass
from datetime import datetime

from woassi.shared.domain.values import Amount

DEFAULT_LANGUAGE = "fr"
INVOICE_TEMPLATE = "woassi_invoice_ready"
NO_VAT_MENTION = "NE FACTURE PAS LA TVA"


@dataclass(frozen=True, slots=True)
class TemplateText:
    """A template the billing module needs Meta to approve for every shop."""

    name: str
    language: str
    body: str
    examples: dict[str, str]


_EXAMPLES = {
    "invoice_number": "FAC-2026-000042",
    "total": "22 500 FCFA",
    "link": "https://api.woassi.com/invoices/abc123",
}

TEMPLATES = (
    TemplateText(
        name=INVOICE_TEMPLATE,
        language="fr",
        # Meta refuses a body that ends with a parameter.
        body=(
            "Merci pour votre paiement ! Votre facture {{invoice_number}} ({{total}}) "
            "est disponible ici : {{link}} . Conservez-la pour vos dossiers."
        ),
        examples=_EXAMPLES,
    ),
    TemplateText(
        name=INVOICE_TEMPLATE,
        language="en",
        body=(
            "Thank you for your payment! Your invoice {{invoice_number}} ({{total}}) "
            "is available here: {{link}} . Keep it for your records."
        ),
        examples=_EXAMPLES,
    ),
)

_TEXTS: dict[str, dict[str, str]] = {
    "fr": {
        "caption": "Votre facture {number} ({total}). Merci pour votre achat !",
        "invoice": "FACTURE",
        "credit_note": "AVOIR",
        "paid_stamp": "PAYÉE",
        "number": "N°",
        "issued_at": "Date d'émission",
        "order": "Commande n°{number}",
        "seller": "Vendeur",
        "buyer": "Client",
        "customer": "Client",
        "tax_id": "NIF",
        "trade_register": "RCCM",
        "tax_regime": "Régime fiscal",
        "phone": "Tél.",
        "item": "Désignation",
        "quantity": "Qté",
        "unit_price": "P.U. TTC",
        "excl_tax": "Montant HT",
        "vat_rate": "TVA",
        "line_total": "Total TTC",
        "total_excl_tax": "Total HT",
        "total_tax": "Total TVA",
        "total": "Total TTC",
        "vat_on": "TVA {rate} sur {base}",
        "paid_on": "Payée le {date} ({method})",
        "refers_to": "Avoir sur la facture {number}",
        "reason": "Motif",
        "refund_reference": "Référence du remboursement",
        "certification": "Certification OTR",
        "not_certified": "Document non soumis à la certification électronique",
        "certified_number": "N° certifié",
        "amount_line": "Remboursement sur la facture {number}",
        "prices_note": "Montants en francs CFA (XOF), prix TTC.",
        "method.kprimepay": "paiement en ligne",
        "method.manual": "paiement reçu par le vendeur",
        "regime.TPU": "TPU",
        "regime.REAL": "Réel",
        "buyer_type.BUSINESS": "Entreprise",
        "buyer_type.INDIVIDUAL": "Particulier",
    },
    "en": {
        "caption": "Your invoice {number} ({total}). Thank you for your purchase!",
        "invoice": "INVOICE",
        "credit_note": "CREDIT NOTE",
        "paid_stamp": "PAID",
        "number": "No.",
        "issued_at": "Issue date",
        "order": "Order #{number}",
        "seller": "Seller",
        "buyer": "Customer",
        "customer": "Customer",
        "tax_id": "Tax ID (NIF)",
        "trade_register": "RCCM",
        "tax_regime": "Tax regime",
        "phone": "Phone",
        "item": "Item",
        "quantity": "Qty",
        "unit_price": "Unit price incl. tax",
        "excl_tax": "Excl. tax",
        "vat_rate": "VAT",
        "line_total": "Total incl. tax",
        "total_excl_tax": "Total excl. tax",
        "total_tax": "Total VAT",
        "total": "Total incl. tax",
        "vat_on": "VAT {rate} on {base}",
        "paid_on": "Paid on {date} ({method})",
        "refers_to": "Credit note on invoice {number}",
        "reason": "Reason",
        "refund_reference": "Refund reference",
        "certification": "OTR certification",
        "not_certified": "Document not subject to electronic certification",
        "certified_number": "Certified No.",
        "amount_line": "Refund on invoice {number}",
        "prices_note": "Amounts in CFA francs (XOF), prices include tax.",
        "method.kprimepay": "online payment",
        "method.manual": "payment received by the seller",
        "regime.TPU": "TPU",
        "regime.REAL": "Real regime",
        "buyer_type.BUSINESS": "Business",
        "buyer_type.INDIVIDUAL": "Individual",
    },
}


def language_of(language: str) -> str:
    """`language` if supported, else French."""
    return language if language in _TEXTS else DEFAULT_LANGUAGE


def text(language: str, key: str, **values: object) -> str:
    """Text `key` in `language` (French if unsupported), filled with `values`. `key` if none."""
    template = _TEXTS[language_of(language)].get(key, key)
    return template.format(**values) if values else template


def amount(value: int) -> str:
    """`12 500 FCFA`, like the orders and payments messages."""
    return str(Amount(value))


def rate(rate_bp: int) -> str:
    """`18 %` (fr) or `18%`-like display of a rate in basis points, without trailing zeros."""
    percent = rate_bp / 100
    shown = f"{percent:.2f}".rstrip("0").rstrip(".")
    return f"{shown} %"


def date(value: datetime) -> str:
    """`10/11/2026 14:05`, already in local time."""
    return value.strftime("%d/%m/%Y %H:%M")
