"""Normalisation of contact values. Raw input is always stored alongside."""

import re

_EMAIL = re.compile(r"^[^@\s<>()\[\],;:\\\"]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$")


def normalize_email(value: str) -> str | None:
    """Lower-cased address, or None when the text is not a plausible email.

    Syntax only: no deliverability or reserved-domain policy is applied.
    """
    candidate = value.strip().lower()
    if len(candidate) > 254 or ".." in candidate or not _EMAIL.match(candidate):
        return None
    return candidate


# Country calling codes for countries the product is configured for. A number is only
# converted to international form when the country is known; a code is never guessed.
CALLING_CODES: dict[str, str] = {
    "bulgaria": "359",
    "bg": "359",
    "romania": "40",
    "ro": "40",
    "greece": "30",
    "gr": "30",
    "germany": "49",
    "de": "49",
    "united kingdom": "44",
    "gb": "44",
    "uk": "44",
}
_PHONE_CHARS = re.compile(r"^[\d\s().+\-/]+$")


def normalize_phone(raw: str, country: str | None = None) -> tuple[str | None, bool]:
    """Return ``(normalized, is_e164)``.

    ``+359888123456`` when the number is international or the country is known;
    otherwise the national digits unchanged, flagged as not E.164.
    """
    text = raw.strip()
    if not text or not _PHONE_CHARS.match(text):
        return None, False
    digits = re.sub(r"\D", "", text)
    if not 5 <= len(digits) <= 15:
        return None, False
    if text.startswith("+"):
        return f"+{digits}", True
    if digits.startswith("00") and len(digits) > 7:
        return f"+{digits[2:]}", True
    code = CALLING_CODES.get((country or "").strip().lower())
    if code and digits.startswith("0"):
        return f"+{code}{digits[1:]}", True
    return digits, False


_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


def normalize_url(raw: str) -> str | None:
    """An http(s) URL with a lower-cased host, or None when it is not one."""
    from urllib.parse import urlsplit, urlunsplit

    text = raw.strip()
    if not text or " " in text:
        return None
    if not _SCHEME.match(text):
        text = f"https://{text}"
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() not in ("http", "https") or not _HOST.match(host):
        return None
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


def domain_of(url: str | None) -> str | None:
    from urllib.parse import urlsplit

    if not url:
        return None
    normalized = normalize_url(url)
    if normalized is None:
        return None
    host = urlsplit(normalized).hostname or ""
    return host.removeprefix("www.") or None
