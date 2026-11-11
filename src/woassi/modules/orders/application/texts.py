"""Every message the orders module sends, in each supported language.

Customer messages are free-form: the customer has just written, so the 24-hour window is open.
The merchant gets the `order_validation` template, as their number is usually outside it.
Template parameters may not contain line breaks, hence the one-line summaries. Being at most
`MAX_PARAMETER` long, they also keep every free-form text far below WhatsApp's 4096 characters.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from woassi.modules.orders.domain.order import OrderLine
from woassi.shared.domain.values import Amount

DEFAULT_LANGUAGE = "fr"
VALIDATION_TEMPLATE = "woassi_order_validation"
MAX_PARAMETER = 600


@dataclass(frozen=True, slots=True)
class TemplateText:
    """A template the orders module needs Meta to approve for every shop."""

    name: str
    language: str
    body: str
    examples: dict[str, str]
    buttons: tuple[str, ...]


_VALIDATION_EXAMPLES = {
    "order_number": "42",
    "customer_name": "Ama",
    "items": "2 x Pagne wax, 1 x Sac",
    "total": "22 500 FCFA",
}

TEMPLATES = (
    TemplateText(
        name=VALIDATION_TEMPLATE,
        language="fr",
        body=(
            "Nouvelle commande n°{{order_number}} de {{customer_name}} : {{items}}. "
            "Total : {{total}}. Pouvez-vous la confirmer ?"
        ),
        examples=_VALIDATION_EXAMPLES,
        buttons=("Valider", "Indisponible"),
    ),
    TemplateText(
        name=VALIDATION_TEMPLATE,
        language="en",
        body=(
            "New order #{{order_number}} from {{customer_name}}: {{items}}. "
            "Total: {{total}}. Can you confirm it?"
        ),
        examples=_VALIDATION_EXAMPLES,
        buttons=("Accept", "Unavailable"),
    ),
)

_TEXTS: dict[str, dict[str, str]] = {
    "fr": {
        "pending": (
            "Merci ! Votre commande n°{number} ({total}) est bien reçue. "
            "Le commerçant va la confirmer."
        ),
        "confirmed": (
            "Votre commande n°{number} est confirmée : {items}. Total : {total}.\n"
            "Vous allez recevoir les instructions de paiement."
        ),
        "out_of_stock": "Désolé, les articles de votre commande ne sont plus disponibles.",
        "offer": (
            "Certains articles ne sont plus disponibles : {missing}. "
            "Voulez-vous commander le reste ({available}) pour {total} ?"
        ),
        "offer_yes": "Oui, commander",
        "offer_no": "Non, annuler",
        "refused": "Désolé, le commerçant ne peut pas honorer votre commande n°{number}.",
        "cancelled": "Votre commande n°{number} est annulée.",
        "expired": "Votre commande n°{number} a expiré, faute de confirmation.",
        "customer": "un client",
    },
    "en": {
        "pending": (
            "Thank you! Your order #{number} ({total}) has been received. "
            "The merchant will confirm it."
        ),
        "confirmed": (
            "Your order #{number} is confirmed: {items}. Total: {total}.\n"
            "You will receive the payment instructions shortly."
        ),
        "out_of_stock": "Sorry, the items of your order are no longer available.",
        "offer": (
            "Some items are no longer available: {missing}. "
            "Would you like to order the rest ({available}) for {total}?"
        ),
        "offer_yes": "Yes, order",
        "offer_no": "No, cancel",
        "refused": "Sorry, the merchant cannot fulfil your order #{number}.",
        "cancelled": "Your order #{number} is cancelled.",
        "expired": "Your order #{number} expired, as it was not confirmed.",
        "customer": "a customer",
    },
}


def text(language: str, key: str, **values: object) -> str:
    """Message `key` in `language` (French if unsupported), filled with `values`."""
    texts = _TEXTS.get(language, _TEXTS[DEFAULT_LANGUAGE])
    return texts[key].format(**values)


def summary(lines: Sequence[OrderLine], *, requested: bool = False) -> str:
    """'2 x Pagne, 1 x Sac' on one line, cut to what a template parameter accepts.

    A long cart keeps whole items and ends with how many were left out: `…, … (+3)`.
    """
    parts = [
        f"{line.requested if requested else line.quantity} x {line.name}".replace("\n", " ")
        for line in lines
    ]
    joined = ", ".join(parts)
    if len(joined) <= MAX_PARAMETER:
        return joined
    for kept in range(len(parts) - 1, 0, -1):
        cut = f"{', '.join(parts[:kept])}, … (+{len(parts) - kept})"
        if len(cut) <= MAX_PARAMETER:
            return cut
    left_out = len(parts) - 1
    return parts[0][: MAX_PARAMETER - 12] + "…" + (f" (+{left_out})" if left_out else "")


def amount(value: Amount) -> str:
    """An amount as shown in messages: `12 500 FCFA`."""
    return str(value)
