"""`InvoiceCertifier` adapters.

Togo's certified e-invoice (loi de finances 2026, art. 62 LPF) is announced but not live: no
platform, API, format or editor approval exists yet. Until it does, no shop is in scope. The
OTR adapter will replace this one in the container, driven by a per-shop setting, without any
change to the domain.
"""

from uuid import UUID

from woassi.modules.billing.domain.document import (
    BillingDocument,
    Certification,
    CertificationStatus,
)


class NoCertificationRequired:
    """Every document is NOT_REQUIRED: released as soon as it is issued."""

    async def required(self, shop_id: UUID) -> bool:
        return False

    async def certify(self, document: BillingDocument) -> Certification:
        return Certification(status=CertificationStatus.NOT_REQUIRED)
