"""Authentication: password hashing, session management, FastAPI dependencies."""

import hashlib
import hmac
import secrets
import logging
from dataclasses import dataclass

from fastapi import Request, HTTPException, Depends

from . import db

log = logging.getLogger("app")

# PBKDF2-SHA256 with 260k iterations (OWASP 2023 guidance)
PBKDF2_ITERATIONS = 260000


def hash_password(password: str) -> str:
    """Hash password in self-describing format: pbkdf2_sha256$iterations$salt_hex$hash_hex."""
    salt = secrets.token_bytes(32)
    hash_obj = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${hash_obj.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    """Verify password against stored hash."""
    try:
        parts = password_hash.split("$")
        if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
            return False
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        stored_hash = parts[3]

        hash_obj = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        return hmac.compare_digest(hash_obj.hex(), stored_hash)
    except Exception as e:
        log.warning("Password verification error: %s", e)
        return False


@dataclass
class AuthedUser:
    """Authenticated user with basic info."""
    id: int
    username: str
    is_admin: bool


async def require_user(request: Request) -> AuthedUser:
    """FastAPI dependency: extract and validate session cookie.

    Returns AuthedUser or raises 401 HTTPException.
    """
    session_id = request.cookies.get("session")
    if not session_id:
        raise HTTPException(status_code=401, detail="No session cookie")

    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    user = db.get_user_by_id(session["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return AuthedUser(id=user["id"], username=user["username"], is_admin=bool(user["is_admin"]))


async def require_admin(user: AuthedUser = Depends(require_user)) -> AuthedUser:
    """FastAPI dependency: require authenticated admin user."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin required")
    return user
