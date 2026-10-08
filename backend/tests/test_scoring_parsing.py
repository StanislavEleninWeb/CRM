"""Pure rules: scoring and the parsers that turn free-text cells into structured values."""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from app.modules.research import parsing
from app.modules.research.importer import FIELDS, detect_mapping
from app.modules.research.scoring import Rubric, ScoreError, compute_score

RUBRIC = Rubric()
FULL = {"evidence": 28, "relevance": 23, "value": 15, "reachability": 14, "activity": 9}


def test_total_and_tier_are_computed_from_components() -> None:
    score = compute_score(FULL, RUBRIC)
    assert score is not None and score.total == 89 and score.tier == "A"
    assert RUBRIC.maximum == 100


@pytest.mark.parametrize(("total", "tier"), [(100, "A"), (80, "A"), (79, "B"), (60, "B"), (59, "C"), (0, "C")])
def test_tier_boundaries(total: int, tier: str) -> None:
    assert RUBRIC.tier_for(total) == tier
    # Reach the same total through components, so the whole path is exercised.
    remaining, components = total, {}
    for key, _, cap in RUBRIC.components:
        components[key] = min(cap, remaining)
        remaining -= components[key]
    score = compute_score(components, RUBRIC)
    assert score is not None and (score.total, score.tier) == (total, tier)


def test_unassessed_is_unscored_not_zero() -> None:
    assert compute_score({}, RUBRIC) is None
    assert compute_score(dict.fromkeys(RUBRIC.keys), RUBRIC) is None
    zero = compute_score(dict.fromkeys(RUBRIC.keys, 0), RUBRIC)
    assert zero is not None and zero.total == 0 and zero.tier == "C"


@pytest.mark.parametrize(
    "components",
    [
        {**FULL, "evidence": 31},
        {**FULL, "activity": 11},
        {**FULL, "value": -1},
        {**FULL, "relevance": 12.5},
        {**FULL, "relevance": True},
        {**FULL, "relevance": "23"},
        {k: v for k, v in FULL.items() if k != "activity"},
        {**FULL, "luck": 5},
    ],
    ids=["over-cap", "over-small-cap", "negative", "fraction", "boolean", "text", "partial", "unknown-component"],
)
def test_invalid_components_are_rejected(components: dict[str, object]) -> None:
    with pytest.raises(ScoreError):
        compute_score(components, RUBRIC)


def test_a_custom_rubric_changes_caps_and_thresholds() -> None:
    rubric = Rubric(components=(("fit", "Fit", 50), ("reach", "Reach", 50)), tier_a_min=90, tier_b_min=50)
    score = compute_score({"fit": 45, "reach": 44}, rubric)
    assert score is not None and score.total == 89 and score.tier == "B"


# --- parsing ---------------------------------------------------------------------------------


def test_missing_markers_are_states_not_values() -> None:
    assert parsing.missing_state("Not found") == {"state": "not_found", "raw": "Not found"}
    assert parsing.missing_state("  not FOUND ") == {"state": "not_found", "raw": "not FOUND"}
    assert parsing.missing_state(None) == {"state": "blank", "raw": ""}
    assert parsing.missing_state("office@example.bg") is None
    assert parsing.parse_emails("Not found") == []
    assert parsing.split_values("Not found") == []


def test_phone_cells_split_into_typed_entries() -> None:
    plain = parsing.parse_phones("+359 2 900 0001; +359 890 00 00 02")
    assert [(p.raw, p.purpose) for p in plain] == [("+359 2 900 0001", "general"), ("+359 890 00 00 02", "general")]
    delivery = parsing.parse_phones("+359 882 00 00 05 (delivery: +359 888 00 00 06)")
    assert [(p.raw, p.purpose, p.label) for p in delivery] == [
        ("+359 882 00 00 05", "general", None),
        ("+359 888 00 00 06", "delivery", "delivery"),
    ]
    emergency = parsing.parse_phones("0887 000 003 (emergency: 0884 000 004)")
    assert [(p.raw, p.purpose) for p in emergency] == [("0887 000 003", "general"), ("0884 000 004", "emergency")]
    assert parsing.parse_phones("0888 1 (fax: 02 111)")[1].purpose == "unknown"  # an unknown label is not guessed
    assert parsing.parse_phones("Not found") == []


@pytest.mark.parametrize(
    ("raw", "base", "availability", "transport", "flags"),
    [
        ("Domain does not resolve", "unreachable", "unavailable", "unknown", ["dns_failure"]),
        (
            "HTTPS connection failed (TLS 'unrecognized name' error) on two attempts",
            "unreachable",
            "unavailable",
            "https_broken",
            ["tls_error"],
        ),
        ("Loads", "loads", "available", "unknown", []),
        ("Loads (HTTP only)", "loads", "available", "http_only", []),
        ("Loads (HTTPS)", "loads", "available", "https", []),
        ("Loads (HTTPS); indexed English homepage returns 404", "loads", "available", "https", ["page_404"]),
        ("Loads (HTTPS); indexed English page returns 404", "loads", "available", "https", ["page_404"]),
        ("Loads (HTTPS); older indexed URL returns 404", "loads", "available", "https", ["page_404"]),
        ("Loads (HTTPS); second domain returns server error", "loads", "available", "https", ["second_domain_error"]),
        ("Loads (HTTPS); single page", "loads", "available", "https", ["single_page"]),
        ("Loads (HTTPS); some assets broken", "loads", "available", "https", ["asset_broken"]),
        (
            "Loads over HTTP only (HTTPS redirects back to HTTP)",
            "loads",
            "available",
            "http_only",
            ["https_redirect_downgrade"],
        ),
        (
            "Loads unrelated content (domain appears expired or compromised)",
            "unrelated_content",
            "available",
            "unknown",
            ["unrelated_content", "domain_expired_or_compromised"],
        ),
        ("No own website found", "not_found", "none", "unknown", []),
    ],
)
def test_website_status_is_a_base_plus_flags(
    raw: str, base: str, availability: str, transport: str, flags: list[str]
) -> None:
    status = parsing.parse_website_status(raw)
    assert (status.raw, status.base, status.availability, status.transport, status.flags) == (
        raw,
        base,
        availability,
        transport,
        flags,
    )


def test_unrecognised_website_status_is_kept_as_written() -> None:
    status = parsing.parse_website_status("Under construction since 2019")
    assert status.base == "unknown" and status.raw == "Under construction since 2019"
    assert parsing.parse_website_status(None).raw is None


def test_channel_recommendations() -> None:
    def parsed(text: str) -> tuple[str | None, list[str], str | None]:
        r = parsing.parse_channel_recommendation(text)
        return r.preferred, r.fallbacks, r.instruction

    assert parsed("Phone") == ("phone", [], None)
    assert parsed("Email") == ("email", [], None)
    assert parsed("Email, then phone") == ("email", ["phone"], None)
    assert parsed("Phone (ask for the manager)") == ("phone", [], "ask for the manager")
    assert parsed("Carrier pigeon") == (None, [], None)
    assert parsing.parse_channel_recommendation("Phone (ask for the manager)").raw == "Phone (ask for the manager)"


def test_listing_identifiers_are_typed() -> None:
    assert parsing.parse_listing("https://www.google.com/maps/place/?q=place_id:ChIJsyntheticPlaceId00000001") == (
        "https://www.google.com/maps/place/?q=place_id:ChIJsyntheticPlaceId00000001",
        "place_id",
        "ChIJsyntheticPlaceId00000001",
    )
    _url, kind, identifier = parsing.parse_listing("https://maps.google.com/maps?cid=1234567890123456789")
    assert (kind, identifier) == ("cid", "1234567890123456789")  # never treated as a place ID
    assert parsing.parse_listing("https://example.bg/map")[1:] == ("unknown", None)
    assert parsing.parse_listing("Not found") == (None, "unknown", None)


def test_date_only_values_never_shift_across_time_zones() -> None:
    midnight = datetime(2026, 10, 7, 0, 0)
    assert parsing.parse_date_only(midnight) == date(2026, 10, 7)
    # The same wall-clock date tagged with far-apart zones still yields the written date.
    for zone in (UTC, timezone(timedelta(hours=3)), timezone(timedelta(hours=-8)), timezone(timedelta(hours=14))):
        assert parsing.parse_date_only(midnight.replace(tzinfo=zone)) == date(2026, 10, 7)
    assert parsing.parse_date_only("2026-10-07") == date(2026, 10, 7)
    assert parsing.parse_date_only("2026-10-07 00:00:00") == date(2026, 10, 7)
    assert parsing.parse_date_only("07.10.2026") == date(2026, 10, 7)
    assert parsing.parse_date_only(None) is None
    with pytest.raises(ValueError, match="not a date"):
        parsing.parse_date_only("yesterday")


def test_reference_headers_map_to_all_35_fields() -> None:
    from app.modules.research.exporter import LEAD_HEADERS

    headers = [h.replace("{tenant}", "SEWEB") for h in LEAD_HEADERS]
    mapping = detect_mapping(headers)
    assert len(headers) == 35 and len(mapping) == 35 and set(mapping) == set(FIELDS)
    assert mapping["phone"] == "Public business phone"
    assert mapping["channel"] == "Recommended contact channel"
    assert mapping["other_channel"] == "Other public business contact channel"
    assert mapping["score_total"] == "Priority score, 0–100"
    assert mapping["score_value"] == "Score: business value (0–20)"
    assert mapping["website_url"] == "Website URL" and mapping["website_status"] == "Website status"
    assert mapping["recommended_service"] == "Recommended SEWEB service"
    assert mapping["service_category"] == "Service category"
    # Another tenant's wording maps the same way.
    assert detect_mapping(["Recommended Acme service", "Score: relevance to Acme (0–25)"]) == {
        "recommended_service": "Recommended Acme service",
        "score_relevance": "Score: relevance to Acme (0–25)",
    }
