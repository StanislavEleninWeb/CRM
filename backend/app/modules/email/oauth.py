"""Google OAuth for the internal Gmail pilot.

The Google Cloud project is an *Internal* app of one Workspace organisation. Two layers
keep it that way: Google only lets that organisation's users sign in, and this module
only lets allow-listed tenants connect, and only an account whose verified ``hd`` claim
is the configured domain. The internal app is never a route for another tenant.
"""

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.modules.email.gmail import REQUIRED_SCOPES, SCOPES, MailboxError

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint, not a credential
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")


class MailboxNotAvailable(AppError):
    status_code = 409
    code = "mailbox_not_available"


@dataclass(frozen=True)
class GoogleAccount:
    email: str
    subject: str
    hosted_domain: str | None
    refresh_token: str
    scopes: frozenset[str]


def ensure_tenant_may_connect(settings: Settings, tenant_id: str) -> None:
    """The admission check for connecting Gmail. Every connect and callback path calls it."""
    if not (settings.gmail_client_id and settings.gmail_client_secret and settings.gmail_internal_domain):
        raise MailboxNotAvailable("Gmail is not configured for this installation.")
    if tenant_id in settings.gmail_internal_tenants:
        return
    if settings.external_gmail_enabled:
        # Reserved for after Google's verification and security assessment. Not reachable today.
        raise MailboxNotAvailable("External Gmail connections are not implemented yet.")
    raise MailboxNotAvailable("Gmail is available only to the organisation that owns this installation's Google app.")


class GoogleAuth:
    def __init__(self, settings: Settings | None = None, http: httpx.Client | None = None) -> None:
        self._settings = settings or get_settings()
        self._http = http or httpx.Client(timeout=15)
        self._jwks: tuple[float, dict[str, Any]] | None = None

    @property
    def redirect_uri(self) -> str:
        return f"{self._settings.public_base_url.rstrip('/')}/api/v1/mailboxes/gmail/callback"

    def authorization_url(self, *, state: str, nonce: str) -> str:
        query = {
            "client_id": self._settings.gmail_client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "false",
            "state": state,
            "nonce": nonce,
            "hd": self._settings.gmail_internal_domain,
        }
        return f"{AUTH_URL}?{urlencode(query)}"

    def exchange(self, *, code: str, nonce: str) -> GoogleAccount:
        try:
            response = self._http.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                    "client_id": self._settings.gmail_client_id,
                    "client_secret": self._settings.gmail_client_secret,
                },
            )
        except httpx.HTTPError as exc:
            raise MailboxNotAvailable("Google could not be reached. Try again.") from exc
        if response.status_code != 200:
            raise MailboxNotAvailable("Google did not accept the authorisation. Start again.")
        data = response.json()
        claims = self._verify_id_token(data.get("id_token") or "", nonce)
        scopes = frozenset((data.get("scope") or "").split())
        if not scopes >= REQUIRED_SCOPES:
            raise MailboxNotAvailable("Both sending and reading permission are needed. Reconnect and allow both.")
        if not data.get("refresh_token"):
            raise MailboxNotAvailable(
                "Google did not return long-lived access. Remove the app's access in your Google account and reconnect."
            )
        domain = self._settings.gmail_internal_domain.lower()
        # ``hd`` is asserted by Google for Workspace accounts. The email suffix alone is never trusted.
        if (claims.get("hd") or "").lower() != domain or claims.get("email_verified") is not True:
            raise MailboxNotAvailable(f"Only verified {domain} accounts can connect a mailbox here.")
        return GoogleAccount(
            email=str(claims["email"]).lower(),
            subject=str(claims["sub"]),
            hosted_domain=claims.get("hd"),
            refresh_token=data["refresh_token"],
            scopes=scopes,
        )

    def _keys(self, refresh: bool = False) -> jwt.PyJWKSet:
        if refresh or not self._jwks or time.monotonic() - self._jwks[0] > 3600:
            response = self._http.get(JWKS_URL)
            response.raise_for_status()
            self._jwks = (time.monotonic(), response.json())
        return jwt.PyJWKSet.from_dict(self._jwks[1])

    def _decode(self, token: str, audience: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256":
                raise jwt.InvalidAlgorithmError("algorithm not allowed")
            key = None
            for refresh in (False, True):
                key = next((k.key for k in self._keys(refresh).keys if k.key_id == header.get("kid")), None)
                if key is not None:
                    break
            if key is None:
                raise jwt.InvalidKeyError("no matching signing key")
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=audience,
                leeway=30,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except (jwt.PyJWTError, httpx.HTTPError) as exc:
            raise MailboxNotAvailable("Google's response could not be verified.") from exc
        if claims.get("iss") not in ISSUERS:
            raise MailboxNotAvailable("Google's response could not be verified.")
        return claims

    def _verify_id_token(self, token: str, nonce: str) -> dict[str, Any]:
        claims = self._decode(token, self._settings.gmail_client_id)
        if not nonce or claims.get("nonce") != nonce:
            raise MailboxNotAvailable("Google's response could not be verified.")
        return claims

    def verify_push(self, authorization: str | None) -> bool:
        """A Pub/Sub push carries a Google-signed token naming the configured service account."""
        settings = self._settings
        if not (
            authorization
            and authorization.lower().startswith("bearer ")
            and settings.gmail_push_audience
            and settings.gmail_push_service_account
        ):
            return False
        try:
            claims = self._decode(authorization[7:].strip(), settings.gmail_push_audience)
        except MailboxNotAvailable:
            return False
        return claims.get("email") == settings.gmail_push_service_account and claims.get("email_verified") is True

    def refresh(self, refresh_token: str) -> str:
        try:
            response = self._http.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._settings.gmail_client_id,
                    "client_secret": self._settings.gmail_client_secret,
                },
            )
        except httpx.HTTPError as exc:
            raise MailboxError("Google could not be reached.") from exc
        if response.status_code in (400, 401) and "invalid_grant" in response.text:
            raise MailboxError("The mailbox authorisation was withdrawn or expired.", revoked=True)
        if response.status_code != 200:
            raise MailboxError("Google refused to refresh the mailbox authorisation.")
        return str(response.json()["access_token"])


_auth: GoogleAuth | None = None


def get_google_auth() -> GoogleAuth:
    global _auth
    if _auth is None:
        _auth = GoogleAuth()
    return _auth


def set_google_auth(auth: GoogleAuth | None) -> None:
    global _auth
    _auth = auth


def refresh_access_token(refresh_token: str) -> str:
    return get_google_auth().refresh(refresh_token)
