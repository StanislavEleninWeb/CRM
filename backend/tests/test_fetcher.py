"""The guarded page fetcher: what it refuses, and that it connects only to the address it checked."""

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpcore
import httpx
import pytest

from app.modules.discovery import fetcher
from app.modules.discovery.extract import extract_facts
from app.modules.discovery.fetcher import (
    FetchBlocked,
    checked_addresses,
    fetch_page,
    guarded_client,
    is_public_address,
    validate_url,
)

PUBLIC = "93.184.216.34"


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.1",
        "172.16.5.4",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.169.254",
        "0.0.0.0",
        "100.64.0.1",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "fe80::1",
        "fc00::1",
        "fd12:3456::1",
        "::ffff:10.0.0.1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "::",
        "ff02::1",
        "2002:0a00:0001::1",
        "192.0.2.1",
        "not-an-ip",
    ],
)
def test_non_public_addresses_are_refused(address: str) -> None:
    assert is_public_address(address) is False


@pytest.mark.parametrize("address", [PUBLIC, "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_public_address(address) is True


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/",
        "javascript:alert(1)",
        "http://",
        "http://user:pw@example.com/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.1.2.3:8080/",
        "http://localhost/",
        "http://db.internal/",
        "http://printer.local/",
        "http://example.com:22/",
        "http://example.com:6379/",
        "http://[::ffff:10.0.0.1]/",
    ],
)
def test_urls_refused_before_any_network_activity(url: str) -> None:
    with pytest.raises(FetchBlocked):
        validate_url(url)
    result = fetch_page(url, resolver=lambda host, port: pytest.fail("the resolver must not be consulted"))
    assert result.limitation and result.limitation.startswith("blocked") and result.status is None and result.html == ""


def test_a_host_with_any_internal_address_is_refused() -> None:
    assert checked_addresses("ok.test", 443, lambda h, p: [PUBLIC]) == [PUBLIC]
    for answers in (["10.0.0.5"], [PUBLIC, "10.0.0.5"], ["::1"], ["169.254.169.254"], []):
        with pytest.raises(FetchBlocked):
            checked_addresses("evil.test", 443, lambda h, p, a=answers: a)
    blocked = fetch_page("https://evil.test/", resolver=lambda h, p: ["192.168.0.10"])
    assert blocked.limitation == "blocked: evil.test resolves to a non-public address"


@pytest.fixture
def site() -> Iterator[tuple[int, list[str]]]:
    """A local web server. The guard would refuse its address, so tests redirect the socket, not the check."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(f"{self.headers.get('Host')} {self.path}")
            if self.path == "/to-internal-ip":
                self._redirect("http://10.0.0.5/admin")
            elif self.path == "/to-internal-name":
                self._redirect("http://rebind.test/secret")
            elif self.path == "/to-file":
                self._redirect("file:///etc/passwd")
            elif self.path == "/loop":
                self._redirect("/loop")
            elif self.path == "/moved":
                self._redirect("/page")
            elif self.path == "/pdf":
                self._send(200, b"%PDF-1.4", "application/pdf")
            elif self.path == "/huge":
                self._send(200, b"<html><body>" + b"x" * (fetcher.MAX_BYTES + 5000), "text/html")
            elif self.path == "/missing":
                self._send(404, b"<html>gone</html>", "text/html")
            else:
                self._send(
                    200,
                    "<html lang='bg'><head><title>Салон Аврора</title></head><body>Добре дошли</body></html>".encode(),
                    "text/html; charset=utf-8",
                )

        def _redirect(self, location: str) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_port, seen
    server.shutdown()


@pytest.fixture
def wired(site: tuple[int, list[str]], monkeypatch: pytest.MonkeyPatch) -> tuple[list[tuple[str, int]], list[str], Any]:
    """Route every *approved* connection to the local server, recording the address the guard chose."""
    port, seen = site
    connected: list[tuple[str, int]] = []
    real = httpcore.SyncBackend.connect_tcp

    def connect(self: Any, host: str, port_: int, **kwargs: Any) -> Any:
        connected.append((host, port_))
        return real(self, "127.0.0.1", port, **kwargs)

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    answers = {"shop.test": [[PUBLIC]], "rebind.test": [["10.0.0.9"]]}
    calls: list[str] = []

    def resolver(host: str, port_: int) -> list[str]:
        calls.append(host)
        queue = answers.get(host, [[PUBLIC]])
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return connected, calls, resolver


def test_fetch_connects_to_the_checked_address_and_reads_html(wired: Any, site: tuple[int, list[str]]) -> None:
    connected, calls, resolver = wired
    result = fetch_page("http://shop.test/page", resolver=resolver)
    assert result.ok and result.status == 200 and "Салон Аврора" in result.html and result.tls is False
    assert connected == [(PUBLIC, 80)]  # the socket went to the address that was checked, not to a second lookup
    assert calls == ["shop.test"]  # resolved once per connection
    assert site[1] == ["shop.test /page"]
    assert extract_facts(result.html, result.final_url or "").language == "bg"


def test_dns_rebinding_cannot_move_the_connection_inside(wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The name answers with a public address when checked and an internal one afterwards."""
    connected, calls, _ = wired
    answers = iter([[PUBLIC], ["10.0.0.9"], ["127.0.0.1"]])
    result = fetch_page("http://shop.test/page", resolver=lambda h, p: next(answers))
    assert result.ok and connected == [(PUBLIC, 80)]
    # A fresh connection re-checks; when the answer has turned internal it is refused, not followed.
    second = fetch_page("http://shop.test/page", resolver=lambda h, p: next(answers))
    assert second.limitation == "blocked: shop.test resolves to a non-public address" and len(connected) == 1


def test_redirects_are_checked_hop_by_hop(wired: Any, site: tuple[int, list[str]]) -> None:
    connected, _, resolver = wired
    followed = fetch_page("http://shop.test/moved", resolver=resolver)
    assert (
        followed.ok
        and followed.final_url == "http://shop.test/page"
        and followed.redirects == ["http://shop.test/page"]
    )
    for path, expected in (
        ("/to-internal-ip", "blocked: the address is not public"),
        ("/to-internal-name", "blocked: rebind.test resolves to a non-public address"),
        ("/to-file", "blocked: only http and https are fetched, not file"),
        ("/loop", "too many redirects"),
    ):
        result = fetch_page(f"http://shop.test{path}", resolver=resolver)
        assert result.limitation == expected and result.html == "", path
    assert all(host == PUBLIC for host, _ in connected)  # nothing ever connected to 10.x
    assert not any("admin" in line or "secret" in line for line in site[1])


def test_only_bounded_html_is_read_and_failures_are_limitations_not_verdicts(wired: Any) -> None:
    _, _, resolver = wired
    pdf = fetch_page("http://shop.test/pdf", resolver=resolver)
    assert pdf.limitation == "not an HTML page (application/pdf)" and pdf.html == ""
    huge = fetch_page("http://shop.test/huge", resolver=resolver)
    assert huge.truncated and len(huge.html.encode()) <= fetcher.MAX_BYTES and huge.ok
    missing = fetch_page("http://shop.test/missing", resolver=resolver)
    assert (
        missing.status == 404
        and missing.limitation == "the page returned HTTP 404 to an automated request"
        and not missing.ok
    )
    dead = fetch_page(
        "http://nowhere.test/",
        resolver=lambda h, p: (_ for _ in ()).throw(FetchBlocked("the host name does not resolve: nowhere.test")),
    )
    assert dead.limitation == "blocked: the host name does not resolve: nowhere.test"


def test_timeouts_and_connection_errors_are_reported_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "slow" in request.url.host:
            raise httpx.ReadTimeout("slow", request=request)
        raise httpx.ConnectError("refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert fetch_page("https://slow.example.com/", client=client).limitation == "the automated request timed out"
    assert (
        fetch_page("https://down.example.com/", client=client).limitation
        == "the automated request failed (ConnectError)"
    )
    assert guarded_client().headers["user-agent"].startswith("SEWEB-CRM-Research")


def test_page_facts_come_from_markup_only_and_scripts_are_ignored() -> None:
    html = """
    <html lang="bg-BG"><head><title>Зоо кабинет Лапа</title>
      <meta name="description" content="Грижа за домашни любимци">
      <link rel="canonical" href="/"><link rel="alternate" hreflang="en" href="/en/">
      <script>document.title = "HACKED"; fetch("http://169.254.169.254/")</script>
      <style>.x{}</style></head>
    <body><nav><a href="/kontakti">Контакти</a> <a href="/zapazi-chas">Запази час</a></nav>
      <p>Работим всеки ден.</p> <a href="tel:+359 2 000 0001">Обадете се</a> <a href="mailto:Office@Lapa.example.bg?subject=x">Пишете ни</a>
      <footer>© 2019 Лапа</footer></body></html>
    """
    facts = extract_facts(html, "https://lapa.example.bg/")
    assert facts.title == "Зоо кабинет Лапа" and facts.language == "bg" and facts.copyright_year == 2019
    assert facts.phones == ["+359 2 000 0001"] and facts.emails == ["office@lapa.example.bg"]
    assert facts.booking_links == ["https://lapa.example.bg/zapazi-chas"] and facts.contact_links == [
        "https://lapa.example.bg/kontakti"
    ]
    assert facts.canonical_url == "https://lapa.example.bg/" and facts.alternate_languages == ["en"]
    assert "HACKED" not in facts.text and "169.254" not in facts.text and "Работим всеки ден." in facts.text
    assert extract_facts("<<<not html", "https://x.example.bg/").title == ""
