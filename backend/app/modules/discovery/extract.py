"""Deterministic facts from fetched HTML. No scripts are run; only text and attributes are read."""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

MAX_TEXT = 20_000
_SKIP = {"script", "style", "noscript", "template", "svg", "iframe"}
_BOOKING = re.compile(r"\b(book(ing)?|reserv|appointment|запази|резерв|запис(ване)?)\b", re.IGNORECASE)
_CONTACT = re.compile(r"\b(contact|kontakt|контакт)", re.IGNORECASE)
_YEAR = re.compile(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(20\d{2})", re.IGNORECASE)


@dataclass
class PageFacts:
    url: str
    title: str = ""
    language: str | None = None
    description: str = ""
    text: str = ""
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    booking_links: list[str] = field(default_factory=list)
    contact_links: list[str] = field(default_factory=list)
    canonical_url: str | None = None
    copyright_year: int | None = None
    alternate_languages: list[str] = field(default_factory=list)


class _Parser(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base, self.facts = base, PageFacts(url=base)
        self._skip = 0
        self._in_title = False
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag in _SKIP:
            self._skip += 1
        elif tag == "html" and a.get("lang"):
            self.facts.language = a["lang"].split("-")[0].lower()[:5]
        elif tag == "title":
            self._in_title = True
        elif tag == "meta" and a.get("name", "").lower() == "description":
            self.facts.description = a.get("content", "")[:500]
        elif tag == "link" and a.get("rel", "").lower() == "canonical" and a.get("href"):
            self.facts.canonical_url = urljoin(self.base, a["href"])
        elif tag == "link" and a.get("rel", "").lower() == "alternate" and a.get("hreflang"):
            self.facts.alternate_languages.append(a["hreflang"].lower()[:10])
        elif tag == "a" and a.get("href"):
            href = a["href"].strip()
            if href.lower().startswith("tel:"):
                self.facts.phones.append(href[4:].strip())
            elif href.lower().startswith("mailto:"):
                self.facts.emails.append(href[7:].split("?")[0].strip().lower())
            else:
                target = urljoin(self.base, href)
                if urlsplit(target).scheme in ("http", "https"):
                    label = f"{href} {a.get('title', '')} {a.get('aria-label', '')}"
                    self._pending_link = (target, label)
                    return
        self._pending_link = None

    _pending_link: tuple[str, str] | None = None

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "a":
            self._pending_link = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.facts.title = (self.facts.title + " " + text).strip()[:300]
            return
        self._chunks.append(text)
        if self._pending_link:
            target, label = self._pending_link
            combined = f"{label} {text}"
            if _BOOKING.search(combined) and target not in self.facts.booking_links:
                self.facts.booking_links.append(target)
            elif _CONTACT.search(combined) and target not in self.facts.contact_links:
                self.facts.contact_links.append(target)


def extract_facts(html: str, url: str) -> PageFacts:
    parser = _Parser(url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup: keep whatever was read  # noqa: S110
        pass
    facts = parser.facts
    facts.text = re.sub(r"\s+", " ", " ".join(parser._chunks))[:MAX_TEXT]
    year = _YEAR.search(html)
    if year:
        facts.copyright_year = int(year.group(1))
    facts.phones = list(dict.fromkeys(p for p in facts.phones if p))[:10]
    facts.emails = list(dict.fromkeys(e for e in facts.emails if "@" in e))[:10]
    return facts
