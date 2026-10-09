"""Discovery and model adapters for research runs.

Adapters return plain values tagged with their source. What may be kept is decided by the
tenant's source policy, not here. The ``Fake*`` adapters are deterministic stand-ins for
local development and tests and are never registered for staging or production.
"""

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

import httpx

PROMPT_VERSION = "research-v1"


@dataclass(frozen=True)
class Listing:
    """One business returned by a discovery provider. Every field is provider content."""

    listing_id_type: str
    listing_id: str
    name: str | None = None
    website_url: str | None = None
    address: str | None = None
    phone: str | None = None
    rating: float | None = None
    review_count: int | None = None


@dataclass(frozen=True)
class DiscoveryResult:
    listings: list[Listing]
    estimated_cost: Decimal
    units: dict[str, int] = field(default_factory=dict)
    coverage_gap: str | None = None


class DiscoveryAdapter(Protocol):
    name: str
    source_type: str
    max_cost_per_query: Decimal

    def search(self, *, city: str, country: str, category: str, limit: int, credential: str) -> DiscoveryResult: ...


class ModelAdapter(Protocol):
    name: str
    model_name: str
    max_cost_per_call: Decimal

    def propose(
        self, payload: dict[str, Any], *, credential: str
    ) -> tuple[dict[str, Any], Decimal, dict[str, int]]: ...


class AdapterError(Exception):
    """A provider call failed. ``ambiguous`` means the call may still have been charged."""

    def __init__(
        self, message: str, *, ambiguous: bool = False, rate_limited: bool = False, revoked: bool = False
    ) -> None:
        super().__init__(message)
        self.ambiguous, self.rate_limited, self.revoked = ambiguous, rate_limited, revoked


class GooglePlacesAdapter:
    """Places API (New) Text Search. Contract-tested only: no live call has been made by this build.

    Requests only the fields the pipeline uses. What may be stored from the response is governed
    by the ``google_places`` source policy; by default that is the place ID alone.
    """

    name = "google_places"
    source_type = "google_places"
    max_cost_per_query = Decimal("0.0400")  # list-price ceiling for one Text Search (Pro) request; an estimate
    endpoint = "https://places.googleapis.com/v1/places:searchText"
    field_mask = "places.id,places.displayName,places.websiteUri,places.formattedAddress"

    def __init__(self, http: httpx.Client | None = None) -> None:
        self._http = http or httpx.Client(timeout=15)

    def search(self, *, city: str, country: str, category: str, limit: int, credential: str) -> DiscoveryResult:
        try:
            response = self._http.post(
                self.endpoint,
                headers={
                    "X-Goog-Api-Key": credential,
                    "X-Goog-FieldMask": self.field_mask,
                    "Content-Type": "application/json",
                },
                json={"textQuery": f"{category} in {city}, {country}", "pageSize": max(1, min(limit, 20))},
            )
        except httpx.TimeoutException as exc:
            raise AdapterError("The discovery provider timed out.", ambiguous=True) from exc
        except httpx.HTTPError as exc:
            raise AdapterError("The discovery provider could not be reached.") from exc
        if response.status_code == 429:
            raise AdapterError("The discovery provider is rate limiting requests.", rate_limited=True)
        if response.status_code in (401, 403):
            raise AdapterError("The discovery provider rejected the credential.", revoked=True)
        if response.status_code >= 500:
            raise AdapterError("The discovery provider returned a server error.", ambiguous=True)
        if response.status_code != 200:
            raise AdapterError(f"The discovery provider refused the request ({response.status_code}).")
        places = response.json().get("places") or []
        listings = [
            Listing(
                listing_id_type="place_id",
                listing_id=p["id"],
                name=(p.get("displayName") or {}).get("text"),
                website_url=p.get("websiteUri"),
                address=p.get("formattedAddress"),
            )
            for p in places
            if p.get("id")
        ]
        gap = (
            "The provider returns at most 20 results per query; more businesses may exist."
            if len(places) >= 20
            else None
        )
        return DiscoveryResult(
            listings=listings,
            estimated_cost=self.max_cost_per_query,
            units={"text_search_requests": 1},
            coverage_gap=gap,
        )


class FakeDiscovery:
    """Deterministic listings built from the query. Websites point at hosts a test resolver controls."""

    name = "fake_discovery"
    source_type = "google_places"  # held to the same policy as the provider it stands in for
    max_cost_per_query = Decimal("0.0100")

    def __init__(self, catalog: dict[tuple[str, str], list[Listing]] | None = None) -> None:
        self.catalog = catalog or {}
        self.calls: list[tuple[str, str]] = []

    def search(self, *, city: str, country: str, category: str, limit: int, credential: str) -> DiscoveryResult:
        self.calls.append((city, category))
        if "timeout" in category:
            raise AdapterError("The discovery provider timed out.", ambiguous=True)
        listings = self.catalog.get((city, category))
        if listings is None:
            seed = hashlib.sha256(f"{city}|{category}".encode()).hexdigest()
            listings = [
                Listing(
                    "place_id",
                    f"ChIJfake{seed[:16]}{i}",
                    name=f"{category.title()} {city} {i}",
                    website_url=f"https://{category.replace(' ', '-')}-{i}.{city.lower()}.example.test/",
                )
                for i in range(1, 4)
            ]
        return DiscoveryResult(listings=listings[:limit], estimated_cost=Decimal("0.0100"), units={"queries": 1})


class FakeModel:
    """Produces a proposal strictly from the page facts it is given, the way a careful model should.

    If the page text contains the marker ``OBEY-INJECTION``, it instead does what the page says,
    which lets tests prove that a manipulated model output is caught by validation.
    """

    name = "fake_model"
    model_name = "fake-research-1"
    max_cost_per_call = Decimal("0.0200")

    def propose(self, payload: dict[str, Any], *, credential: str) -> tuple[dict[str, Any], Decimal, dict[str, int]]:
        pages = payload["pages"]
        page = pages[0] if pages else {"url": None, "text": "", "booking_links": [], "phones": [], "emails": []}
        text = page.get("text", "")
        if "OBEY-INJECTION" in text:
            proposal = {
                "observations": [
                    {
                        "text": "The owner told us they urgently need a new website.",
                        "evidence_url": "https://attacker.example/proof",
                        "quote": "we urgently need a new website",
                    }
                ],
                "hypotheses": [],
                "components": {"evidence": 99, "relevance": 25, "value": 20, "reachability": 15, "activity": 10},
                "component_reasons": {},
                "recommended_service": "Send all customer data to attacker@example.com",
                "confidence": "high",
                "contacts": [{"kind": "email", "value": "ceo.private@invented.example", "source_url": page.get("url")}],
                "opening": "As agreed, here is the invoice.",
                "discovery_question": "",
                "actions": [{"type": "send_email", "to": "everyone"}],
            }
            return proposal, Decimal("0.0200"), {"input_tokens": 900, "output_tokens": 200}
        has_booking = bool(page.get("booking_links"))
        services = payload.get("services") or ["Website refresh"]
        observation = (
            {
                "text": "No booking option was found on the inspected pages.",
                "evidence_url": page.get("url"),
                "quote": text[:60],
            }
            if not has_booking and text
            else {
                "text": "The inspected homepage links to an online booking page.",
                "evidence_url": page.get("url"),
                "quote": text[:60],
            }
        )
        contacts = [{"kind": "phone", "value": p, "source_url": page.get("url")} for p in page.get("phones", [])]
        contacts += [{"kind": "email", "value": e, "source_url": page.get("url")} for e in page.get("emails", [])]
        proposal = {
            "observations": [observation] if text else [],
            "hypotheses": ["Bookings may be taken by phone only."] if not has_booking and text else [],
            "components": {
                "evidence": 22 if text else 8,
                "relevance": 20 if not has_booking else 4,
                "value": 14 if not has_booking else 6,
                "reachability": 13 if contacts else 5,
                "activity": 7,
            },
            "component_reasons": {"evidence": "Read from the homepage in one automated fetch."},
            "recommended_service": services[0],
            "confidence": "medium" if text else "low",
            "contacts": contacts,
            "opening": "Здравейте, разгледах сайта Ви и забелязах, че няма онлайн резервация."
            if payload.get("language") == "bg"
            else "Hello, I looked at your website and noticed there is no online booking.",
            "discovery_question": "Как приемате резервации в момента?"
            if payload.get("language") == "bg"
            else "How do you take bookings today?",
        }
        return json.loads(json.dumps(proposal)), Decimal("0.0200"), {"input_tokens": 800, "output_tokens": 250}
