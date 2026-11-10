"""Use cases the messaging api needs, provided by the composition root."""

from typing import Protocol

from woassi.modules.messaging.application.use_cases.webhook import (
    ReceiveMetaWebhook,
    VerifyMetaSubscription,
)


class MessagingUseCases(Protocol):
    """What this module expects from the composition root (see catalog.application.facade)."""

    def verify_meta_subscription(self) -> VerifyMetaSubscription: ...

    def receive_meta_webhook(self) -> ReceiveMetaWebhook: ...
