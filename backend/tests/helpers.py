"""Helpers that drive the real sign-in flow against the development identity provider."""

import re
from html import unescape
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.security import CSRF_COOKIE

DEV_PASSWORD = "dev-password"
API = "/api/v1"


def _internal(url: str) -> str:
    settings = get_settings()
    assert settings.oidc_internal_url
    if url.startswith(settings.oidc_issuer):
        return settings.oidc_internal_url + url[len(settings.oidc_issuer) :]
    return url


def provider_login(authorize_url: str, email: str, password: str = DEV_PASSWORD) -> str:
    """Complete the provider's login form and return the redirect back to the app."""
    with httpx.Client(follow_redirects=False, timeout=10) as http:
        url = _internal(authorize_url)
        response = http.get(url)
        for _ in range(6):
            if response.status_code in (301, 302, 303, 307):
                location = urljoin(url, response.headers["location"])
                if location.startswith(get_settings().public_base_url):
                    return location
                url = _internal(location)
                response = http.get(url)
                continue
            assert response.status_code == 200, response.text[:300]
            match = re.search(r'<form[^>]*action="([^"]+)"', response.text)
            assert match, "login form not found"
            url = _internal(urljoin(url, unescape(match.group(1))))
            response = http.post(url, data={"login": email, "password": password})
        raise AssertionError("login flow did not complete")


def begin_login(client: TestClient, return_to: str | None = None) -> str:
    params = {"return_to": return_to} if return_to else None
    response = client.get(f"{API}/auth/login", params=params, follow_redirects=False)
    assert response.status_code == 307, response.text
    return str(response.headers["location"])


def callback_path(redirect: str) -> str:
    parts = urlsplit(redirect)
    return f"{parts.path}?{parts.query}"


def sign_in(client: TestClient, email: str) -> None:
    redirect = provider_login(begin_login(client), email)
    response = client.get(callback_path(redirect), follow_redirects=False)
    assert response.status_code == 303, response.text
    client.headers["X-CSRF-Token"] = client.cookies[CSRF_COOKIE]


def state_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


def create_workspace(client: TestClient, name: str) -> str:
    response = client.post(f"{API}/tenants", json={"name": name})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def invite(client: TestClient, email: str, role: str) -> dict[str, str]:
    response = client.post(f"{API}/invitations", json={"email": email, "role": role})
    assert response.status_code == 201, response.text
    body: dict[str, str] = response.json()
    return body


def join(owner: TestClient, member: TestClient, email: str, role: str) -> None:
    token = invite(owner, email, role)["token"]
    response = member.post(f"{API}/invitations/accept", json={"token": token})
    assert response.status_code == 200, response.text
