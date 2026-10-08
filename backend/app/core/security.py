"""Opaque tokens, hashing and cookies."""

import base64
import hashlib
import hmac
import secrets

from fastapi import Response

from app.core.config import get_settings

SESSION_COOKIE = "crm_session"
CSRF_COOKIE = "crm_csrf"
LOGIN_COOKIE = "crm_login"
CSRF_HEADER = "x-csrf-token"


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> bytes:
    """Tokens are high-entropy random values, so a plain digest is sufficient."""
    return hashlib.sha256(token.encode()).digest()


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def set_cookie(
    response: Response, name: str, value: str, *, max_age: int, http_only: bool = True
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=http_only,
        secure=get_settings().cookies_secure,
        samesite="lax",
        path="/",
    )


def clear_cookie(response: Response, name: str) -> None:
    response.delete_cookie(name, path="/", secure=get_settings().cookies_secure, samesite="lax")
