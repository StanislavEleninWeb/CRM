"""Fetching third-party web pages safely.

Every connection is made through a guard that resolves the host, refuses private,
loopback, link-local, metadata and other non-public addresses, and then connects to the
exact address it checked. Because the check and the connection use the same address,
a DNS answer that changes between them (rebinding) cannot redirect the request inward.
Redirects are followed by hand so each hop goes through the same guard.

Pages are data. Nothing fetched here is executed, and nothing but HTML is read.
"""

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

MAX_BYTES = 1_500_000
MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 10.0
USER_AGENT = "SEWEB-CRM-Research/1.0 (+business website check)"
ALLOWED_PORTS = {80, 443, 8080, 8443}
Resolver = Callable[[str, int], Iterable[str]]


class FetchBlocked(Exception):
    """The request was refused before any bytes were sent to the target."""


def is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:10.0.0.1 is 10.0.0.1
    if isinstance(ip, ipaddress.IPv6Address) and ip.sixtofour is not None:
        ip = ip.sixtofour
    # ``is_global`` excludes private, loopback, link-local (including the cloud metadata address),
    # shared carrier space, documentation ranges, unspecified and reserved addresses.
    return ip.is_global and not ip.is_multicast


def system_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchBlocked(f"the host name does not resolve: {host}") from exc
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def checked_addresses(host: str, port: int, resolver: Resolver) -> list[str]:
    """Resolve ``host`` and insist that every address it maps to is public."""
    addresses = list(resolver(host, port))
    if not addresses:
        raise FetchBlocked(f"the host name does not resolve: {host}")
    bad = [a for a in addresses if not is_public_address(a)]
    if bad:
        # One internal address among several is enough to refuse: an attacker controls the answer.
        raise FetchBlocked(f"{host} resolves to a non-public address")
    return addresses


class _GuardedBackend(httpcore.SyncBackend):
    def __init__(self, resolver: Resolver) -> None:
        super().__init__()
        self._resolver = resolver

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        address = checked_addresses(host, port, self._resolver)[0]
        return super().connect_tcp(
            address, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.NetworkStream:
        raise FetchBlocked("unix sockets are not allowed")


def guarded_client(resolver: Resolver = system_resolver) -> httpx.Client:
    transport = httpx.HTTPTransport(retries=0)
    transport._pool = httpcore.ConnectionPool(network_backend=_GuardedBackend(resolver), max_connections=4, retries=0)
    return httpx.Client(
        transport=transport,
        follow_redirects=False,
        timeout=TIMEOUT_SECONDS,
        trust_env=False,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )


def validate_url(url: str) -> str:
    """Scheme, host and port rules that apply before any network activity."""
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise FetchBlocked("the address is not a valid URL") from exc
    if parts.scheme not in ("http", "https"):
        raise FetchBlocked(f"only http and https are fetched, not {parts.scheme or 'this'}")
    if not parts.hostname or parts.username or parts.password:
        raise FetchBlocked("the address has no host or carries credentials")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise FetchBlocked(f"port {port} is not fetched")
    host = parts.hostname
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not is_public_address(host):
        raise FetchBlocked("the address is not public")
    if host.endswith((".local", ".internal", ".localhost")) or host == "localhost":
        raise FetchBlocked("internal host names are not fetched")
    return url


@dataclass
class FetchResult:
    requested_url: str
    final_url: str | None = None
    status: int | None = None
    html: str = ""
    truncated: bool = False
    redirects: list[str] = field(default_factory=list)
    tls: bool = False
    limitation: str | None = None  # why the page could not be assessed; never implies the site is down

    @property
    def ok(self) -> bool:
        return self.limitation is None and self.status is not None and 200 <= self.status < 300


def fetch_page(url: str, *, client: httpx.Client | None = None, resolver: Resolver = system_resolver) -> FetchResult:
    """Fetch one HTML page with size, time and redirect limits. Never raises for target-side problems."""
    result = FetchResult(requested_url=url)
    own_client = client is None
    http = client or guarded_client(resolver)
    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            try:
                validate_url(current)
            except FetchBlocked as exc:
                result.limitation = f"blocked: {exc}"
                return result
            try:
                with http.stream("GET", current) as response:
                    if response.status_code in (301, 302, 303, 307, 308) and "location" in response.headers:
                        current = urljoin(current, response.headers["location"])
                        result.redirects.append(current)
                        continue
                    result.status, result.final_url = response.status_code, current
                    result.tls = current.startswith("https://")
                    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if content_type not in ("text/html", "application/xhtml+xml", ""):
                        result.limitation = f"not an HTML page ({content_type})"
                        return result
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            chunks.append(chunk[: MAX_BYTES - (size - len(chunk))])
                            result.truncated = True
                            break
                        chunks.append(chunk)
                    result.html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                    if not 200 <= response.status_code < 300:
                        result.limitation = f"the page returned HTTP {response.status_code} to an automated request"
                    return result
            except FetchBlocked as exc:
                result.limitation = f"blocked: {exc}"
                return result
            except httpx.TimeoutException:
                result.limitation = "the automated request timed out"
                return result
            except (httpx.HTTPError, httpcore.ConnectError, OSError) as exc:
                result.limitation = f"the automated request failed ({type(exc).__name__})"
                return result
        result.limitation = "too many redirects"
        return result
    finally:
        if own_client:
            http.close()
