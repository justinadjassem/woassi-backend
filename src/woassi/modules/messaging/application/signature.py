"""Webhook signature: an HMAC-SHA256 of the raw body, keyed with the Meta app secret."""

import hashlib
import hmac

PREFIX = "sha256="


def is_valid_signature(body: bytes, header: str | None, secret: str) -> bool:
    """Check the X-Hub-Signature-256 header against the raw request body."""
    if not header or not header.startswith(PREFIX):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # As bytes: on str, compare_digest raises TypeError if the header is not ASCII.
    return hmac.compare_digest(header.removeprefix(PREFIX).encode(), expected.encode())
