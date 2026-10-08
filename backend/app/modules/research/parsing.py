"""Turning free-text research cells into structured values.

Every function is pure and keeps nothing: callers store the original text beside the
structured result. Nothing here guesses a value that the text does not contain.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

MISSING_MARKERS = {"not found", "n/a", "na", "none", "-", "—", "unknown"}
_SPLIT = re.compile(r"\s*;\s*")
_LABELLED = re.compile(r"\(\s*([A-Za-zА-Яа-я ]{2,30})\s*:\s*([^()]+?)\s*\)")
_PURPOSES = {
    "delivery": "delivery",
    "deliveries": "delivery",
    "orders": "delivery",
    "emergency": "emergency",
    "emergencies": "emergency",
    "booking": "booking",
    "bookings": "booking",
    "reservations": "booking",
    "reception": "general",
    "office": "general",
}


def clean(value: Any) -> str | None:
    """Trimmed text, or None for an empty cell."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def missing_state(value: Any) -> dict[str, str] | None:
    """``{"state": "not_found", "raw": "Not found"}`` when a cell records an explicit gap."""
    text = clean(value)
    if text is None:
        return {"state": "blank", "raw": ""}
    if text.lower() in MISSING_MARKERS:
        return {"state": "not_found" if text.lower() == "not found" else "not_provided", "raw": text}
    return None


def split_values(value: Any) -> list[str]:
    text = clean(value)
    if text is None or missing_state(text):
        return []
    return [part for part in _SPLIT.split(text) if part]


@dataclass(frozen=True)
class PhoneEntry:
    raw: str
    purpose: str = "general"
    label: str | None = None


def parse_phones(value: Any) -> list[PhoneEntry]:
    """Split a phone cell into typed entries, keeping labels such as delivery and emergency.

    ``0887 000 003 (emergency: 0884 000 004)`` yields a general number and an emergency one.
    """
    entries: list[PhoneEntry] = []
    for part in split_values(value):
        labelled = _LABELLED.findall(part)
        main = _LABELLED.sub("", part).strip(" ,")
        if main:
            entries.append(PhoneEntry(raw=main))
        for label, number in labelled:
            key = label.strip().lower()
            entries.append(PhoneEntry(raw=number.strip(), purpose=_PURPOSES.get(key, "unknown"), label=label.strip()))
    return entries


def parse_emails(value: Any) -> list[str]:
    text = clean(value)
    if text is None or missing_state(text):
        return []
    return [part for part in re.split(r"[;,\s]+", text) if "@" in part]


@dataclass(frozen=True)
class WebsiteStatus:
    raw: str | None
    base: str = "unknown"
    availability: str = "unknown"
    transport: str = "unknown"
    flags: list[str] = field(default_factory=list)
    match: str = "unknown"


def parse_website_status(value: Any) -> WebsiteStatus:
    """A base status plus independent flags. Unrecognised text stays ``unknown`` with its raw value."""
    raw = clean(value)
    if raw is None:
        return WebsiteStatus(raw=None)
    text = raw.lower()
    flags: list[str] = []
    base, availability, transport, match = "unknown", "unknown", "unknown", "unknown"

    if "no own website" in text or text in ("no website found", "not found"):
        return WebsiteStatus(raw=raw, base="not_found", availability="none", match="none")
    if "does not resolve" in text:
        base, availability = "unreachable", "unavailable"
        flags.append("dns_failure")
    elif "connection failed" in text or ("tls" in text and "error" in text):
        base, availability, transport = "unreachable", "unavailable", "https_broken"
        flags.append("tls_error")
    elif "unrelated content" in text:
        base, availability, match = "unrelated_content", "available", "none"
        flags.append("unrelated_content")
    elif text.startswith("loads"):
        base, availability, match = "loads", "available", "assumed"

    if base in ("loads", "unrelated_content"):
        if "http only" in text:
            transport = "http_only"
        elif "(https)" in text or text.startswith("loads (https"):
            transport = "https"
        if "redirects back to http" in text:
            flags.append("https_redirect_downgrade")
    for needle, flag in (
        ("404", "page_404"),
        ("assets broken", "asset_broken"),
        ("single page", "single_page"),
        ("server error", "second_domain_error"),
        ("expired or compromised", "domain_expired_or_compromised"),
    ):
        if needle in text:
            flags.append(flag)
    return WebsiteStatus(raw=raw, base=base, availability=availability, transport=transport, flags=flags, match=match)


@dataclass(frozen=True)
class ChannelRecommendation:
    raw: str | None
    preferred: str | None = None
    fallbacks: list[str] = field(default_factory=list)
    instruction: str | None = None


_CHANNEL_WORDS = {
    "phone": "phone",
    "call": "phone",
    "email": "email",
    "e-mail": "email",
    "contact form": "contact_page",
    "contact page": "contact_page",
    "form": "contact_page",
    "facebook": "social",
    "instagram": "social",
    "social": "social",
}


def parse_channel_recommendation(value: Any) -> ChannelRecommendation:
    """``Email, then phone`` becomes email with a phone fallback; a parenthetical becomes an instruction."""
    raw = clean(value)
    if raw is None:
        return ChannelRecommendation(raw=None)
    instruction = None
    note = re.search(r"\(([^()]+)\)", raw)
    if note:
        instruction = note.group(1).strip()
    body = re.sub(r"\([^()]*\)", "", raw).lower()
    ordered: list[str] = []
    for part in re.split(r",|\bthen\b|\bor\b|/|;", body):
        word = part.strip()
        channel = next((kind for needle, kind in _CHANNEL_WORDS.items() if needle in word), None)
        if channel and channel not in ordered:
            ordered.append(channel)
    if not ordered:
        return ChannelRecommendation(raw=raw, instruction=instruction)
    return ChannelRecommendation(raw=raw, preferred=ordered[0], fallbacks=ordered[1:], instruction=instruction)


def parse_listing(value: Any) -> tuple[str | None, str, str | None]:
    """``(url, identifier_type, identifier)``. A ``cid`` is never reinterpreted as a place ID."""
    url = clean(value)
    if url is None or missing_state(url):
        return None, "unknown", None
    place = re.search(r"place_id[:=]([A-Za-z0-9_-]{10,})", url)
    if place:
        return url, "place_id", place.group(1)
    try:
        query = parse_qs(urlsplit(url).query)
    except ValueError:
        return url, "unknown", None
    cid = query.get("cid", [None])[0]
    if cid and cid.isdigit():
        return url, "cid", cid
    return url, "unknown", None


def parse_date_only(value: Any) -> date | None:
    """A calendar date exactly as written. A midnight timestamp is never shifted through a time zone."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return date(value.year, value.month, value.day)
    if isinstance(value, date):
        return value
    text = str(value).strip()
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    match = re.match(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})$", text)
    if match:
        return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    raise ValueError(f"not a date: {text[:40]}")


def parse_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("not a number")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != int(value):
            raise ValueError("not a whole number")
        return int(value)
    text = str(value).strip()
    if not re.fullmatch(r"-?\d+", text):
        raise ValueError(f"not a whole number: {text[:20]}")
    return int(text)


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
