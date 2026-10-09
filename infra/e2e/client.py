"""A signed-in HTTP client for end-to-end checks against a running local stack.

Runs inside the API container: ``docker compose exec api python /infra/e2e/<script>.py``.
It signs in through the development identity provider exactly as a browser would.
"""

import re
import time
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.config import get_settings

API_URL = "http://localhost:8000"
DEV_PASSWORD = "dev-password"  # noqa: S105 - development identity provider only


def _internal(url: str) -> str:
    settings = get_settings()
    if settings.oidc_internal_url and url.startswith(settings.oidc_issuer):
        return settings.oidc_internal_url + url[len(settings.oidc_issuer) :]
    return url


def sign_in(email: str) -> httpx.Client:
    client = httpx.Client(base_url=API_URL, follow_redirects=False, timeout=30)
    start = client.get("/api/v1/auth/login")
    assert start.status_code == 307, start.text
    with httpx.Client(follow_redirects=False, timeout=15) as idp:
        url = _internal(start.headers["location"])
        response = idp.get(url)
        callback = None
        for _ in range(6):
            if response.status_code in (301, 302, 303, 307):
                location = urljoin(url, response.headers["location"])
                if location.startswith(get_settings().public_base_url):
                    callback = location
                    break
                url = _internal(location)
                response = idp.get(url)
                continue
            form = re.search(r'<form[^>]*action="([^"]+)"', response.text)
            assert form, "login form not found"
            url = _internal(urljoin(url, unescape(form.group(1))))
            response = idp.post(url, data={"login": email, "password": DEV_PASSWORD})
        assert callback, "sign-in did not complete"
    parts = urlsplit(callback)
    done = client.get(f"{parts.path}?{parts.query}")
    assert done.status_code == 303, done.text
    client.headers["X-CSRF-Token"] = client.cookies["crm_csrf"]
    return client


def ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, f"{response.request.method} {response.request.url} -> {response.status_code}: {response.text[:300]}"
    return response.json() if response.content else None


def wait_for(client: httpx.Client, path: str, done: set[str], timeout: float = 90) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body: dict[str, Any] = ok(client.get(path))
        if body["status"] in done:
            return body
        time.sleep(1)
    raise AssertionError(f"{path} did not reach {done}")
