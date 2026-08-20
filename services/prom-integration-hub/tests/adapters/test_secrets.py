"""Round-trip test for the real FernetSecretProvider."""

from __future__ import annotations

from cryptography.fernet import Fernet

from src.adapters.secrets.fernet_provider import FernetSecretProvider


def test_encrypt_decrypt_round_trip() -> None:
    key = Fernet.generate_key().decode("utf-8")
    provider = FernetSecretProvider(key)

    ciphertext = provider.encrypt(b"super-secret-token")

    assert ciphertext != b"super-secret-token"
    assert provider.decrypt(ciphertext) == b"super-secret-token"
