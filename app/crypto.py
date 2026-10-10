"""Encryption at rest for printer access codes and notification credentials."""
from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


class SecretError(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = current_app.config.get("FERNET_KEY")
    if not key:
        raise SecretError("BFC_FERNET_KEY is not configured")
    return Fernet(key.encode())


def encrypt(value: str | None) -> str | None:
    if not value:
        return None
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise SecretError("Stored secret cannot be decrypted; was BFC_FERNET_KEY changed?") from exc
