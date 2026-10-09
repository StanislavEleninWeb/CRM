"""Building outbound messages and making inbound content safe to store and show."""

import re
from email.message import EmailMessage
from email.utils import format_datetime, getaddresses, make_msgid, parseaddr
from html import escape
from html.parser import HTMLParser
from typing import Any

from app.core.normalize import normalize_email
from app.core.time import utcnow

ALLOWED_TAGS = {
    "p",
    "br",
    "div",
    "span",
    "b",
    "strong",
    "i",
    "em",
    "u",
    "ul",
    "ol",
    "li",
    "blockquote",
    "pre",
    "code",
    "a",
    "table",
    "thead",
    "tbody",
    "tr",
    "td",
    "th",
    "h1",
    "h2",
    "h3",
    "h4",
    "hr",
}
VOID_TAGS = {"br", "hr"}
DROP_WITH_CONTENT = {
    "script",
    "style",
    "iframe",
    "object",
    "embed",
    "form",
    "svg",
    "math",
    "head",
    "title",
    "noscript",
    "template",
}
MAX_HTML = 200_000


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.remote = False
        self._drop = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in DROP_WITH_CONTENT:
            self._drop += 1
            return
        if self._drop:
            return
        if tag == "img":
            # Remote images are tracking beacons as often as they are pictures: never loaded.
            self.remote = True
            self.out.append("<span>[image removed]</span>")
            return
        if tag not in ALLOWED_TAGS:
            return
        rendered = ""
        if tag == "a":
            href = next((v for k, v in attrs if k == "href" and v), "") or ""
            if re.match(r"^(https?://|mailto:)", href.strip(), re.IGNORECASE):
                rendered = (
                    f' href="{escape(href.strip(), quote=True)}" rel="noopener noreferrer nofollow" target="_blank"'
                )
        self.out.append(f"<{tag}{rendered}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in DROP_WITH_CONTENT:
            self._drop = max(0, self._drop - 1)
        elif not self._drop and tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._drop:
            self.out.append(escape(data, quote=False))


def sanitize_html(html: str) -> tuple[str, bool]:
    """Keep simple formatting and safe links only. Returns ``(html, had_remote_content)``."""
    parser = _Sanitizer()
    try:
        parser.feed(html[:MAX_HTML])
        parser.close()
    except Exception:
        return "", False
    return "".join(parser.out), parser.remote or bool(re.search(r"url\(|@import", html, re.IGNORECASE))


def addresses(value: str | None) -> list[str]:
    found = [normalize_email(addr) for _, addr in getaddresses([value or ""])]
    return list(dict.fromkeys(a for a in found if a))


def one_address(value: str | None) -> str | None:
    return normalize_email(parseaddr(value or "")[1])


def message_ids(value: str | None) -> list[str]:
    return re.findall(r"<[^<>\s]+>", value or "")


def new_message_id(domain: str) -> str:
    return make_msgid(domain=domain)


def build_message(
    *,
    sender: str,
    sender_name: str | None,
    to: str,
    subject: str,
    body: str,
    message_id: str,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    unsubscribe_url: str | None = None,
    unsubscribe_mailto: str | None = None,
) -> bytes:
    """An RFC 5322 plain-text message. Header values are set through the email library, which refuses line breaks."""
    for label, value in (("recipient", to), ("subject", subject), ("sender", sender)):
        if "\n" in value or "\r" in value:
            raise ValueError(f"the {label} contains a line break")
    message = EmailMessage()
    message["From"] = f"{sender_name} <{sender}>" if sender_name else sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = format_datetime(utcnow())
    message["Message-ID"] = message_id
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = " ".join([*(references or []), in_reply_to][-20:])
    targets = [f"<{t}>" for t in (unsubscribe_url, f"mailto:{unsubscribe_mailto}" if unsubscribe_mailto else None) if t]
    if targets:
        message["List-Unsubscribe"] = ", ".join(targets)
        if unsubscribe_url:
            message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    message.set_content(body, charset="utf-8")
    return message.as_bytes()


def classify(headers: dict[str, str], *, from_address: str | None, subject: str, content_type: str) -> str:
    """Tell an automatic message from a person's. Only headers and well-known subject prefixes are used."""
    local = (from_address or "").split("@")[0].lower()
    auto = headers.get("auto-submitted", "").lower()
    if (
        local in ("mailer-daemon", "postmaster")
        or "multipart/report" in content_type.lower()
        or headers.get("x-failed-recipients")
    ):
        return "bounce"
    if (
        auto.startswith("auto-replied")
        or headers.get("x-autoreply")
        or headers.get("x-autorespond")
        or re.match(
            r"^\s*(out of office|automatic reply|auto[- ]?reply|автоматичен отговор|извън офиса)",
            subject,
            re.IGNORECASE,
        )
    ):
        return "out_of_office"
    if auto.startswith("auto-generated") or headers.get("precedence", "").lower() in ("bulk", "list", "junk"):
        return "auto_generated"
    return "message"


def is_permanent_bounce(text_body: str, headers: dict[str, Any]) -> bool:
    """A 5.x.x status means the address is permanently undeliverable; 4.x.x is temporary."""
    status = re.search(r"\b([245])\.\d{1,3}\.\d{1,3}\b", text_body or "")
    if status:
        return status.group(1) == "5"
    return bool(re.search(r"\b55\d\b", text_body or ""))
