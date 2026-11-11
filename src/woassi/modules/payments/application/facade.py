"""Use cases the payments api needs, provided by the composition root."""

from typing import Protocol

from woassi.modules.payments.application.use_cases.manual import RecordManualPayment
from woassi.modules.payments.application.use_cases.provider import (
    ConfigurePaymentProvider,
    GetPaymentProvider,
    RemovePaymentProvider,
)
from woassi.modules.payments.application.use_cases.queries import GetPayment, ListPayments
from woassi.modules.payments.application.use_cases.requests import RequestPayment
from woassi.modules.payments.application.use_cases.verification import ReceiveKPrimePayWebhook


class PaymentsUseCases(Protocol):
    """What this module expects from the composition root (see catalog.application.facade)."""

    def configure_payment_provider(self) -> ConfigurePaymentProvider: ...

    def get_payment_provider(self) -> GetPaymentProvider: ...

    def remove_payment_provider(self) -> RemovePaymentProvider: ...

    def list_payments(self) -> ListPayments: ...

    def get_payment(self) -> GetPayment: ...

    def record_manual_payment(self) -> RecordManualPayment: ...

    def request_payment(self) -> RequestPayment: ...

    def receive_kprimepay_webhook(self) -> ReceiveKPrimePayWebhook: ...
