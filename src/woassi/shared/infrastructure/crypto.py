"""Secret encryption with Fernet (AES-128-CBC + HMAC-SHA256)."""

from collections.abc import Sequence

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from woassi.shared.application.ports import SecretDecryptionError


class FernetSecretCipher:
    """Encrypts with the first key and decrypts with any of them, which allows key rotation.

    To rotate: put the new key first, re-encrypt stored values with `rotate`, then drop the old key.
    """

    def __init__(self, keys: Sequence[str]) -> None:
        if not keys:
            raise ValueError("At least one encryption key is required.")
        self._fernet = MultiFernet([Fernet(key) for key in keys])

    def encrypt(self, plaintext: str) -> str:
        """Encrypts `plaintext` with the first key."""
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        """Decrypts with whichever key matches; raises SecretDecryptionError otherwise."""
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as error:
            raise SecretDecryptionError("Secret cannot be decrypted.") from error

    def rotate(self, ciphertext: str) -> str:
        """Re-encrypt `ciphertext` with the first key."""
        try:
            return self._fernet.rotate(ciphertext.encode()).decode()
        except InvalidToken as error:
            raise SecretDecryptionError("Secret cannot be decrypted.") from error
