"""Reversible encryption for WebUntis/IHK credentials.

Used to store UNTIS_PASS/IHK_PASS encrypted at rest in the DB (Fernet).
The app needs the plaintext back to actually log into WebUntis/IHK, so this
is reversible encryption, not hashing.
"""

from cryptography.fernet import Fernet, InvalidToken
from . import config


def encrypt(plaintext: str) -> bytes:
    """Encrypt plaintext to bytes via Fernet (SECRET_ENCRYPTION_KEY from env)."""
    if not config.SECRET_ENCRYPTION_KEY:
        raise RuntimeError("SECRET_ENCRYPTION_KEY not set in environment")
    try:
        f = Fernet(config.SECRET_ENCRYPTION_KEY.encode())
        return f.encrypt(plaintext.encode())
    except Exception as e:
        raise RuntimeError(f"encryption failed (bad key?): {e}")


def decrypt(ciphertext: bytes) -> str:
    """Decrypt bytes back to plaintext via Fernet."""
    if not config.SECRET_ENCRYPTION_KEY:
        raise RuntimeError("SECRET_ENCRYPTION_KEY not set in environment")
    try:
        f = Fernet(config.SECRET_ENCRYPTION_KEY.encode())
        return f.decrypt(ciphertext).decode()
    except InvalidToken:
        raise RuntimeError("decryption failed (wrong key or corrupted data)")
    except Exception as e:
        raise RuntimeError(f"decryption failed: {e}")
