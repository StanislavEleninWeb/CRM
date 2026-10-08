"""OpenID Connect authorization-code flow with PKCE.

The provider is reached two ways: the browser uses the public issuer URL, and
the API uses an optional internal URL. The ``iss`` claim is always compared
with the configured public issuer, never with whatever address was dialled.
"""

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from app.core.config import Settings
from app.core.errors import ServiceUnavailableError, UnauthenticatedError
from app.core.logging import get_logger

log = get_logger(__name__)
ALLOWED_ALGORITHMS = ("RS256", "ES256", "PS256")
_CACHE_SECONDS = 300


@dataclass(frozen=True)
class Identity:
    issuer: str
    subject: str
    email: str
    email_verified: bool
    name: str
    mfa: bool


class OidcClient:
    def __init__(self, settings: Settings, http: httpx.Client) -> None:
        self._settings = settings
        self._http = http
        self._discovery: tuple[float, dict[str, Any]] | None = None
        self._jwks: tuple[float, dict[str, Any]] | None = None

    # -- transport ---------------------------------------------------------------------------
    def _back_channel(self, url: str) -> str:
        internal = self._settings.oidc_internal_url
        issuer = self._settings.oidc_issuer
        if internal and url.startswith(issuer):
            return internal.rstrip("/") + url[len(issuer.rstrip("/")) :]
        return url

    def _get_json(self, url: str) -> dict[str, Any]:
        try:
            response = self._http.get(self._back_channel(url), timeout=10)
            response.raise_for_status()
            body: dict[str, Any] = response.json()
            return body
        except (httpx.HTTPError, ValueError) as exc:
            log.error("oidc_unreachable", error=type(exc).__name__)
            raise ServiceUnavailableError("The identity provider is unavailable.") from exc

    def discovery(self) -> dict[str, Any]:
        if self._discovery and time.monotonic() - self._discovery[0] < _CACHE_SECONDS:
            return self._discovery[1]
        issuer = self._settings.oidc_issuer.rstrip("/")
        document = self._get_json(f"{issuer}/.well-known/openid-configuration")
        if document.get("issuer") != self._settings.oidc_issuer:
            raise ServiceUnavailableError("The identity provider reported an unexpected issuer.")
        self._discovery = (time.monotonic(), document)
        return document

    def _signing_keys(self, *, refresh: bool = False) -> jwt.PyJWKSet:
        if refresh or not self._jwks or time.monotonic() - self._jwks[0] >= _CACHE_SECONDS:
            self._jwks = (time.monotonic(), self._get_json(self.discovery()["jwks_uri"]))
        return jwt.PyJWKSet.from_dict(self._jwks[1])

    # -- flow --------------------------------------------------------------------------------
    @property
    def redirect_uri(self) -> str:
        return f"{self._settings.public_base_url.rstrip('/')}/api/v1/auth/callback"

    def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._settings.oidc_client_id,
                "redirect_uri": self.redirect_uri,
                "scope": self._settings.oidc_scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.discovery()['authorization_endpoint']}?{query}"

    def redeem(self, *, code: str, code_verifier: str, nonce: str) -> Identity:
        try:
            response = self._http.post(
                self._back_channel(self.discovery()["token_endpoint"]),
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                    "code_verifier": code_verifier,
                },
                auth=(self._settings.oidc_client_id, self._settings.oidc_client_secret),
                timeout=10,
            )
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("The identity provider is unavailable.") from exc
        if response.status_code != 200:
            log.warning("oidc_code_rejected", status=response.status_code)
            raise UnauthenticatedError("Sign-in could not be completed.")
        id_token = response.json().get("id_token")
        if not isinstance(id_token, str):
            raise UnauthenticatedError("Sign-in could not be completed.")
        return self.validate_id_token(id_token, nonce=nonce)

    def validate_id_token(self, id_token: str, *, nonce: str) -> Identity:
        try:
            header = jwt.get_unverified_header(id_token)
            if header.get("alg") not in ALLOWED_ALGORITHMS:
                raise jwt.InvalidAlgorithmError("algorithm not allowed")
            key = self._find_key(header.get("kid"))
            claims = jwt.decode(
                id_token,
                key=key,
                algorithms=list(ALLOWED_ALGORITHMS),
                audience=self._settings.oidc_client_id,
                issuer=self._settings.oidc_issuer,
                leeway=30,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            log.warning("oidc_token_rejected", reason=type(exc).__name__)
            raise UnauthenticatedError("Sign-in could not be completed.") from exc

        audience = claims["aud"]
        several_audiences = isinstance(audience, list) and len(audience) > 1
        if several_audiences and claims.get("azp") != self._settings.oidc_client_id:
            raise UnauthenticatedError("Sign-in could not be completed.")
        if not nonce or claims.get("nonce") != nonce:
            log.warning("oidc_token_rejected", reason="nonce_mismatch")
            raise UnauthenticatedError("Sign-in could not be completed.")
        email = claims.get("email")
        if not isinstance(email, str) or "@" not in email:
            raise UnauthenticatedError("The identity provider did not supply an email address.")
        amr = claims.get("amr") or []
        return Identity(
            issuer=claims["iss"],
            subject=str(claims["sub"]),
            email=email.strip().lower(),
            email_verified=claims.get("email_verified") is True,
            name=str(claims.get("name") or claims.get("preferred_username") or ""),
            mfa=any(method in ("mfa", "otp", "hwk", "swk") for method in amr),
        )

    def _find_key(self, kid: str | None) -> Any:
        for refresh in (False, True):
            for key in self._signing_keys(refresh=refresh).keys:
                if kid is None or key.key_id == kid:
                    return key.key
        raise jwt.InvalidKeyError("no matching signing key")
