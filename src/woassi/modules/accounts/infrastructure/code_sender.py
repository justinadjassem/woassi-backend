"""Verification code sender for development."""

import structlog

from woassi.modules.accounts.domain.verification_code import CodePurpose
from woassi.shared.domain.values import PhoneNumber

log = structlog.get_logger()


class LogVerificationCodeSender:
    """Writes the code to the logs. For development only: see Container.code_sender."""

    async def send(self, phone: PhoneNumber, code: str, purpose: CodePurpose) -> None:
        """Logs the code in clear text, so a developer can read it."""
        log.info("verification_code", phone=phone.value, code=code, purpose=purpose.value)
