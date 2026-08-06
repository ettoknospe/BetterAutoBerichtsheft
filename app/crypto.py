"""Reversible encryption for WebUntis/IHK credentials and per-user data.

Used to store UNTIS_PASS/IHK_PASS encrypted at rest in the DB (Fernet), and
- via encrypt_with_key/decrypt_with_key - to encrypt each user's week data,
IHK history/status/fields under their own random data-encryption key (DEK),
itself wrapped by SECRET_ENCRYPTION_KEY. The app needs the plaintext back
(to log into WebUntis/IHK, to render week data), so this is reversible
encryption, not hashing.
"""

from cryptography.fernet import Fernet, InvalidToken
from . import config


def generate_dek() -> bytes:
    """Generate a new random per-user data-encryption key (a Fernet key)."""
    return Fernet.generate_key()


def encrypt_with_key(key: bytes, plaintext: str) -> bytes:
    """Encrypt plaintext to bytes via Fernet, using an explicit key."""
    try:
        f = Fernet(key)
        return f.encrypt(plaintext.encode())
    except Exception as e:
        raise RuntimeError(f"encryption failed (bad key?): {e}")


def decrypt_with_key(key: bytes, ciphertext: bytes) -> str:
    """Decrypt bytes back to plaintext via Fernet, using an explicit key."""
    try:
        f = Fernet(key)
        return f.decrypt(ciphertext).decode()
    except InvalidToken:
        raise RuntimeError("decryption failed (wrong key or corrupted data)")
    except Exception as e:
        raise RuntimeError(f"decryption failed: {e}")


def encrypt(plaintext: str) -> bytes:
    """Encrypt plaintext via Fernet using the master SECRET_ENCRYPTION_KEY from env."""
    if not config.SECRET_ENCRYPTION_KEY:
        raise RuntimeError("SECRET_ENCRYPTION_KEY not set in environment")
    return encrypt_with_key(config.SECRET_ENCRYPTION_KEY.encode(), plaintext)


def decrypt(ciphertext: bytes) -> str:
    """Decrypt bytes via Fernet using the master SECRET_ENCRYPTION_KEY from env."""
    if not config.SECRET_ENCRYPTION_KEY:
        raise RuntimeError("SECRET_ENCRYPTION_KEY not set in environment")
    return decrypt_with_key(config.SECRET_ENCRYPTION_KEY.encode(), ciphertext)
