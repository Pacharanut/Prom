"""Fernet-based SecretProvider — encrypts secrets (delegated tokens, channel credentials) at
rest. Generate a key with: python -c "from cryptography.fernet import Fernet;
print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

from cryptography.fernet import Fernet

from src.application.account_linking.ports import SecretProvider


class FernetSecretProvider(SecretProvider):
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode("utf-8"))

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return self._fernet.decrypt(ciphertext)
