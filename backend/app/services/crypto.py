import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings
from app.core.exceptions import DomainError


def _fernet() -> Fernet:
    raw = get_settings().encryption_key.strip()
    if not raw:
        raise DomainError("ENCRYPTION_KEY_MISSING", "APP_ENCRYPTION_KEY must be configured.", status_code=500)
    try:
        return Fernet(raw.encode())
    except (TypeError, ValueError):
        # The development setting may be a passphrase; derive a valid Fernet key without
        # ever storing or returning the original API key.
        derived = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
        return Fernet(derived)


def encrypt_api_key(value: str) -> str:
    if not value.strip():
        raise DomainError("API_KEY_REQUIRED", "An API key is required for this model configuration.", status_code=422)
    return _fernet().encrypt(value.encode()).decode()


def decrypt_api_key(value: str | None) -> str:
    if not value:
        raise DomainError("API_KEY_MISSING", "This model configuration has no API key.", status_code=422)
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, TypeError) as error:
        raise DomainError("API_KEY_DECRYPT_FAILED", "The stored API key cannot be decrypted.", status_code=500) from error
