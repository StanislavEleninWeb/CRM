"""Sign-in, sessions, CSRF and OIDC token validation."""

import json
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.deps import get_redis
from app.core.errors import ServiceUnavailableError, UnauthenticatedError
from app.core.security import LOGIN_COOKIE, SESSION_COOKIE
from app.modules.identity.oidc import OidcClient
from tests.helpers import API, begin_login, callback_path, provider_login, sign_in, state_of


def test_sign_in_through_the_identity_provider(client: TestClient) -> None:
    assert client.get(f"{API}/auth/me").status_code == 401
    sign_in(client, "owner@example.test")
    me = client.get(f"{API}/auth/me").json()
    assert me["user"]["email"] == "owner@example.test"
    assert me["user"]["display_name"] == "Olga Owner"
    assert me["active_tenant"] is None and me["tenants"] == []
    cookie = next(c for c in client.cookies.jar if c.name == SESSION_COOKIE)
    assert cookie.has_nonstandard_attr("HttpOnly")


def test_session_token_is_stored_only_as_a_hash(client: TestClient, migrator_engine: Any) -> None:
    from sqlalchemy import text

    sign_in(client, "owner@example.test")
    token = client.cookies[SESSION_COOKIE]
    with migrator_engine.connect() as conn:
        stored = conn.execute(text("SELECT token_hash FROM sessions")).scalar_one()
    assert token.encode() not in bytes(stored)
    assert len(bytes(stored)) == 32


def test_callback_rejects_state_that_does_not_match_this_browser(make_client: Any) -> None:
    victim, attacker = make_client(), make_client()
    redirect = provider_login(begin_login(attacker), "other@example.test")
    begin_login(victim)  # the victim has their own pending login
    response = victim.get(callback_path(redirect), follow_redirects=False)
    assert response.status_code == 401
    assert SESSION_COOKIE not in victim.cookies


def test_callback_without_login_cookie_is_rejected(make_client: Any) -> None:
    started, fresh = make_client(), make_client()
    redirect = provider_login(begin_login(started), "owner@example.test")
    assert fresh.get(callback_path(redirect), follow_redirects=False).status_code == 401


def test_callback_cannot_be_replayed(client: TestClient) -> None:
    redirect = provider_login(begin_login(client), "owner@example.test")
    state = state_of(redirect)
    assert client.get(callback_path(redirect), follow_redirects=False).status_code == 303
    client.cookies.set(LOGIN_COOKIE, state)
    assert client.get(callback_path(redirect), follow_redirects=False).status_code == 401


def test_pkce_verifier_mismatch_is_rejected_by_the_provider(client: TestClient) -> None:
    authorize_url = begin_login(client)
    key = f"crm:oidc:login:{state_of(authorize_url)}"
    store = get_redis()
    pending = json.loads(store.get(key))  # type: ignore[arg-type]
    pending["verifier"] = "x" * 64
    store.set(key, json.dumps(pending), ex=60)
    redirect = provider_login(authorize_url, "owner@example.test")
    assert client.get(callback_path(redirect), follow_redirects=False).status_code == 401
    assert SESSION_COOKIE not in client.cookies


def test_wrong_password_never_reaches_the_callback(client: TestClient) -> None:
    with pytest.raises(AssertionError):
        provider_login(begin_login(client), "owner@example.test", password="wrong")


def test_return_path_must_be_same_site(client: TestClient) -> None:
    for attempt in ("https://evil.example/x", "//evil.example", "/\\evil.example"):
        redirect = provider_login(begin_login(client, attempt), "owner@example.test")
        response = client.get(callback_path(redirect), follow_redirects=False)
        assert response.headers["location"] == f"{get_settings().public_base_url}/"
    redirect = provider_login(begin_login(client, "/team?tab=1"), "owner@example.test")
    response = client.get(callback_path(redirect), follow_redirects=False)
    assert response.headers["location"] == f"{get_settings().public_base_url}/team?tab=1"


def test_unsafe_requests_need_the_csrf_token(client: TestClient) -> None:
    sign_in(client, "owner@example.test")
    good = client.headers.pop("X-CSRF-Token")
    assert client.post(f"{API}/tenants", json={"name": "Acme"}).status_code == 403
    assert client.post(f"{API}/tenants", json={"name": "Acme"}, headers={"X-CSRF-Token": "nope"}).status_code == 403
    assert client.get(f"{API}/auth/me").status_code == 200  # safe methods are unaffected
    assert client.post(f"{API}/tenants", json={"name": "Acme"}, headers={"X-CSRF-Token": good}).status_code == 201


def test_logout_revokes_the_session(client: TestClient) -> None:
    sign_in(client, "owner@example.test")
    token = client.cookies[SESSION_COOKIE]
    assert client.post(f"{API}/auth/logout").status_code == 204
    client.cookies.set(SESSION_COOKIE, token)  # a stolen copy of the cookie
    assert client.get(f"{API}/auth/me").status_code == 401


def test_sessions_can_be_listed_and_revoked(make_client: Any) -> None:
    laptop, phone = make_client(), make_client()
    sign_in(laptop, "owner@example.test")
    sign_in(phone, "owner@example.test")
    sessions = laptop.get(f"{API}/auth/sessions").json()["items"]
    assert len(sessions) == 2 and sum(s["current"] for s in sessions) == 1
    assert laptop.post(f"{API}/auth/sessions/revoke-others").status_code == 204
    assert phone.get(f"{API}/auth/me").status_code == 401
    assert laptop.get(f"{API}/auth/me").status_code == 200


def test_a_user_cannot_revoke_someone_elses_session(make_client: Any) -> None:
    alice, bob = make_client(), make_client()
    sign_in(alice, "owner@example.test")
    sign_in(bob, "other@example.test")
    target = alice.get(f"{API}/auth/sessions").json()["items"][0]["id"]
    assert bob.delete(f"{API}/auth/sessions/{target}").status_code == 404
    assert alice.get(f"{API}/auth/me").status_code == 200


def test_expired_session_is_rejected(client: TestClient, migrator_engine: Any) -> None:
    from sqlalchemy import text

    sign_in(client, "owner@example.test")
    with migrator_engine.begin() as conn:
        conn.execute(text("UPDATE sessions SET expires_at = now() - interval '1 second'"))
    assert client.get(f"{API}/auth/me").status_code == 401


# --- ID token validation with keys the test controls ---------------------------------------

ISSUER = "https://idp.example.test"
CLIENT_ID = "crm-test"


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://a:b@db/crm",
        session_secret="s" * 40,
        oidc_issuer=ISSUER,
        oidc_internal_url=None,
        oidc_client_id=CLIENT_ID,
        oidc_client_secret="secret",
    )


class FakeProvider:
    def __init__(self, *, discovery_issuer: str = ISSUER) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.discovery_issuer = discovery_issuer
        jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update({"kid": "k1", "use": "sig", "alg": "RS256"})
        self.jwks = {"keys": [jwk]}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": self.discovery_issuer,
                    "authorization_endpoint": f"{ISSUER}/auth",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/keys",
                },
            )
        if request.url.path == "/keys":
            return httpx.Response(200, json=self.jwks)
        return httpx.Response(404)

    def client(self) -> OidcClient:
        return OidcClient(_settings(), httpx.Client(transport=httpx.MockTransport(self.handler)))

    def token(self, *, key: Any = None, algorithm: str = "RS256", **overrides: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "user-1",
            "iat": now,
            "exp": now + 300,
            "nonce": "n-1",
            "email": "User@Example.Test",
            "email_verified": True,
            "name": "Test User",
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, key or self.key, algorithm=algorithm, headers={"kid": "k1"})


def test_valid_id_token_is_accepted_and_email_normalised() -> None:
    provider = FakeProvider()
    identity = provider.client().validate_id_token(provider.token(), nonce="n-1")
    assert identity.email == "user@example.test"
    assert identity.email_verified is True
    assert identity.mfa is False
    with_mfa = provider.client().validate_id_token(provider.token(amr=["pwd", "otp"]), nonce="n-1")
    assert with_mfa.mfa is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://evil.example.test"},
        {"aud": "another-client"},
        {"exp": int(time.time()) - 3600},
        {"nonce": "different"},
        {"nonce": None},
        {"sub": None},
        {"email": None},
        {"aud": [CLIENT_ID, "other"], "azp": "other"},
    ],
    ids=[
        "wrong-iss",
        "wrong-aud",
        "expired",
        "nonce-mismatch",
        "no-nonce",
        "no-sub",
        "no-email",
        "azp",
    ],
)
def test_forged_or_invalid_claims_are_rejected(overrides: dict[str, Any]) -> None:
    provider = FakeProvider()
    with pytest.raises(UnauthenticatedError):
        provider.client().validate_id_token(provider.token(**overrides), nonce="n-1")


def test_token_signed_with_an_unknown_key_is_rejected() -> None:
    provider = FakeProvider()
    with pytest.raises(UnauthenticatedError):
        provider.client().validate_id_token(provider.token(key=provider.other_key), nonce="n-1")


def test_unsigned_token_is_rejected() -> None:
    provider = FakeProvider()
    now = int(time.time())
    unsigned = jwt.encode(
        {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "u",
            "iat": now,
            "exp": now + 60,
            "nonce": "n-1",
            "email": "a@b.test",
        },
        key="",
        algorithm="none",
    )
    with pytest.raises(UnauthenticatedError):
        provider.client().validate_id_token(unsigned, nonce="n-1")


def test_hmac_token_signed_with_the_public_key_is_rejected() -> None:
    """Algorithm-confusion attack: the public key must never be accepted as an HMAC secret."""
    import base64
    import hashlib
    import hmac

    provider = FakeProvider()
    public_pem = provider.key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )

    def b64(data: bytes) -> bytes:
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    now = int(time.time())
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode())
    payload = b64(
        json.dumps(
            {
                "iss": ISSUER,
                "aud": CLIENT_ID,
                "sub": "u",
                "iat": now,
                "exp": now + 60,
                "nonce": "n-1",
                "email": "a@b.test",
            }
        ).encode()
    )
    signature = b64(hmac.new(public_pem, header + b"." + payload, hashlib.sha256).digest())
    forged = (header + b"." + payload + b"." + signature).decode()
    with pytest.raises(UnauthenticatedError):
        provider.client().validate_id_token(forged, nonce="n-1")


def test_discovery_with_unexpected_issuer_is_refused() -> None:
    provider = FakeProvider(discovery_issuer="https://evil.example.test")
    with pytest.raises(ServiceUnavailableError):
        provider.client().discovery()


def test_development_provider_is_refused_in_production() -> None:
    base = {
        "environment": "production",
        "database_url": "postgresql+psycopg://crm_app:real@db/crm",
        "session_secret": "s" * 40,
        "public_base_url": "https://crm.example.test",
        "oidc_issuer": "https://idp.example.test",
        "oidc_client_secret": "real-secret",
        "oidc_dev_provider": False,
        "s3_secret_key": "real-storage-secret",
    }
    Settings(**base)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(**{**base, "oidc_dev_provider": True})  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(**{**base, "oidc_issuer": "http://localhost:5556/dex"})  # type: ignore[arg-type]
