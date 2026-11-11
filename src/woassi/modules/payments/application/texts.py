"""Messages the payments module sends, in each supported language.

Inside the 24-hour window they go as free text or reply buttons; outside it, as the templates
below. Template parameters must be a single line, and Meta refuses a body that starts or ends
with a parameter.
"""

from dataclasses import dataclass

from woassi.shared.domain.values import Amount

DEFAULT_LANGUAGE = "fr"
PAYMENT_TEMPLATE = "woassi_order_payment"  # the checkout link
PUSH_TEMPLATE = "woassi_payment_push"  # confirm the USSD push, or another way
NUMBER_TEMPLATE = "woassi_payment_number"  # which number will pay?


@dataclass(frozen=True, slots=True)
class TemplateText:
    """A template the payments module needs Meta to approve for every shop."""

    name: str
    language: str
    body: str
    examples: dict[str, str]
    buttons: tuple[str, ...] = ()


_LINK_EXAMPLES = {
    "order_number": "42",
    "total": "22 500 FCFA",
    "link": "https://payments.kprimepay.com/checkout/web/abc123",
}
_EXAMPLES = {"order_number": "42", "total": "22 500 FCFA"}

TEMPLATES = (
    TemplateText(
        name=PAYMENT_TEMPLATE,
        language="fr",
        body=(
            "Votre commande n°{{order_number}} est confirmée. Total : {{total}}. "
            "Payez-la en toute sécurité ici : {{link}} . Merci de votre confiance !"
        ),
        examples=_LINK_EXAMPLES,
    ),
    TemplateText(
        name=PAYMENT_TEMPLATE,
        language="en",
        body=(
            "Your order #{{order_number}} is confirmed. Total: {{total}}. "
            "Pay for it securely here: {{link}} . Thank you for your trust!"
        ),
        examples=_LINK_EXAMPLES,
    ),
    TemplateText(
        name=PUSH_TEMPLATE,
        language="fr",
        body=(
            "Commande n°{{order_number}} : confirmez sur votre téléphone la demande de paiement "
            "de {{total}} avec votre code secret. Pour payer depuis un autre numéro ou par carte, "
            "utilisez les boutons ci-dessous."
        ),
        examples=_EXAMPLES,
        buttons=("Autre numéro", "Lien de paiement"),
    ),
    TemplateText(
        name=PUSH_TEMPLATE,
        language="en",
        body=(
            "Order #{{order_number}}: confirm on your phone the payment request of {{total}} "
            "with your PIN. To pay from another number or by card, use the buttons below."
        ),
        examples=_EXAMPLES,
        buttons=("Another number", "Payment link"),
    ),
    TemplateText(
        name=NUMBER_TEMPLATE,
        language="fr",
        body=(
            "Pour payer {{total}} pour la commande n°{{order_number}}, répondez avec le numéro "
            "Mobile Money (Flooz ou Mixx by Yas) qui va payer, ou demandez un lien de paiement."
        ),
        examples=_EXAMPLES,
        buttons=("Lien de paiement",),
    ),
    TemplateText(
        name=NUMBER_TEMPLATE,
        language="en",
        body=(
            "To pay {{total}} for order #{{order_number}}, reply with the Mobile Money number "
            "(Flooz or Mixx by Yas) that will pay, or ask for a payment link."
        ),
        examples=_EXAMPLES,
        buttons=("Payment link",),
    ),
)

_TEXTS: dict[str, dict[str, str]] = {
    "fr": {
        "link": (
            "Pour payer votre commande n°{number} ({total}), utilisez ce lien sécurisé : {link}"
        ),
        "push": (
            "Commande n°{number} : confirmez sur votre téléphone la demande de paiement de "
            "{total}, envoyée au {masked}."
        ),
        "push_sent": "Demande envoyée au {masked} : validez-la avec votre code secret.",
        "push_failed": (
            "La demande de paiement de {total} envoyée au {masked} n'a pas abouti. "
            "Vous pouvez réessayer avec un autre numéro ou payer par lien."
        ),
        "ask_number": (
            "Pour payer {total} pour la commande n°{number}, envoyez le numéro Mobile Money "
            "(Flooz ou Mixx by Yas) qui va payer, par exemple 90 12 34 56."
        ),
        "invalid_number": (
            "Ce numéro n'est pas un numéro Mobile Money togolais (Flooz ou Mixx by Yas). "
            "Envoyez-le par exemple ainsi : 90 12 34 56."
        ),
        "other_number": "Autre numéro",
        "get_link": "Lien de paiement",
        "description": "Commande n°{number} - {shop}",
    },
    "en": {
        "link": "To pay for your order #{number} ({total}), use this secure link: {link}",
        "push": (
            "Order #{number}: confirm on your phone the payment request of {total}, "
            "sent to {masked}."
        ),
        "push_sent": "Request sent to {masked}: approve it with your PIN.",
        "push_failed": (
            "The payment request of {total} sent to {masked} did not go through. "
            "You can try another number or pay by link."
        ),
        "ask_number": (
            "To pay {total} for order #{number}, send the Mobile Money number "
            "(Flooz or Mixx by Yas) that will pay, for instance 90 12 34 56."
        ),
        "invalid_number": (
            "This is not a Togolese Mobile Money number (Flooz or Mixx by Yas). "
            "Send it like this, for instance: 90 12 34 56."
        ),
        "other_number": "Another number",
        "get_link": "Payment link",
        "description": "Order #{number} - {shop}",
    },
}


def language_of(language: str) -> str:
    """`language` if supported, else French."""
    return language if language in _TEXTS else DEFAULT_LANGUAGE


def text(language: str, key: str, **values: object) -> str:
    """Message `key` in `language` (French if unsupported), filled with `values`."""
    return _TEXTS[language_of(language)][key].format(**values)


def amount(value: int) -> str:
    """`12 500 FCFA`."""
    return str(Amount(value))
