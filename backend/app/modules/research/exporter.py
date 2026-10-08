"""Workbook export in the reference layout.

Every text cell is written as an explicit string, so a value that starts with ``=``,
``+``, ``-`` or ``@`` can never be interpreted as a formula, and ordinary values such
as ``+359 2 900 0001`` are exported unchanged.
"""

import io
from collections import Counter
from datetime import date
from typing import Any
from uuid import UUID

from openpyxl import Workbook
from openpyxl.cell.cell import Cell
from openpyxl.styles import Font
from sqlalchemy import text
from sqlalchemy.orm import Session

LEAD_HEADERS = [
    "Lead ID",
    "Business name",
    "Industry / business type",
    "City",
    "Country",
    "Website URL",
    "Google Business Profile / Maps URL",
    "Public business phone",
    "Public business email",
    "Contact page URL",
    "Other public business contact channel",
    "Website status",
    "Specific observed issue or opportunity",
    "Evidence / relevant page URL",
    "Opportunity hypothesis requiring confirmation",
    "Recommended {tenant} service",
    "Why this business is a suitable cold prospect",
    "Suggested business benefit",
    "Personalized outreach opening",
    "Suggested discovery question",
    "Recommended contact channel",
    "Priority score, 0–100",
    "Priority tier: A, B, or C",
    "Evidence confidence: high, medium, or low",
    "Date checked",
    "Outreach status",
    "Notes",
    "Score: evidence strength (0–30)",
    "Score: relevance to {tenant} (0–25)",
    "Score: business value (0–20)",
    "Score: reachability (0–15)",
    "Score: business activity (0–10)",
    "Industry group",
    "Service category",
    "Observed opportunity tags",
]
SHORTLIST_HEADERS = [
    "Rank",
    "Lead ID",
    "Business name",
    "City",
    "Industry / business type",
    "Priority score",
    "Tier",
    "Public business phone",
    "Public business email",
    "Other contact channel",
    "Recommended contact channel",
    "Website URL",
    "Google Maps URL",
    "Strongest evidence (verified finding)",
    "Evidence URL",
    "Recommended {tenant} service",
    "Personalized opening",
    "Evidence confidence",
    "Outreach status",
]
NOT_FOUND = "Not found"

EXPORT_SQL = """
SELECT l.id AS lead_id, l.external_id, l.outreach_status, c.name, c.business_type, c.industry_group, c.city, c.country,
       c.website_url, a.*, s.total, s.tier, s.components, s.source_total, s.origin AS score_origin,
       sv.name AS service_category,
       (SELECT o.text FROM observations o WHERE o.tenant_id = l.tenant_id AND o.lead_id = l.id AND o.superseded_at IS NULL
        ORDER BY o.created_at LIMIT 1) AS observation,
       (SELECT o.evidence_url FROM observations o WHERE o.tenant_id = l.tenant_id AND o.lead_id = l.id AND o.superseded_at IS NULL
        ORDER BY o.created_at LIMIT 1) AS evidence_url,
       (SELECT h.text FROM hypotheses h WHERE h.tenant_id = l.tenant_id AND h.lead_id = l.id AND h.superseded_at IS NULL
        ORDER BY h.created_at LIMIT 1) AS hypothesis,
       (SELECT string_agg(t.name::text, '; ' ORDER BY tg.created_at, t.name::text) FROM taggings tg
        JOIN tags t ON t.tenant_id = tg.tenant_id AND t.id = tg.tag_id
        WHERE tg.tenant_id = l.tenant_id AND tg.entity_type = 'lead' AND tg.entity_id = l.id) AS tags
FROM leads l
JOIN companies c ON c.tenant_id = l.tenant_id AND c.id = l.company_id
LEFT JOIN lead_assessments a ON a.tenant_id = l.tenant_id AND a.lead_id = l.id AND a.is_current
LEFT JOIN lead_scores s ON s.tenant_id = l.tenant_id AND s.lead_id = l.id AND s.is_current
LEFT JOIN service_catalog sv ON sv.tenant_id = l.tenant_id AND sv.id = a.service_id
WHERE l.tenant_id = :t AND c.merged_into_id IS NULL
ORDER BY l.external_id NULLS LAST, l.created_at, l.id
"""


def _write(cell: Cell, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, str):
        cell.value = value
        cell.data_type = "s"  # never a formula, whatever the first character is
    else:
        cell.value = value


def _headers(template: list[str], tenant_name: str) -> list[str]:
    return [h.replace("{tenant}", tenant_name) for h in template]


def _status_text(row: Any) -> str:
    return (
        (row["outreach_status_raw"] or row["outreach_status"].replace("_", " ").capitalize())
        if row["outreach_status"]
        else ""
    )


def _tags_text(row: Any) -> str | None:
    """Tags in their original order when they are unchanged since import; otherwise alphabetical."""
    current = row["tags"]
    if not current:
        return None
    raw = next((v for k, v in (row["raw_row"] or {}).items() if k.lower().endswith("tags")), None)
    if isinstance(raw, str) and {t.strip() for t in raw.split(";") if t.strip()} == {
        t.strip() for t in current.split(";")
    }:
        return raw
    return str(current)


def build_export(
    db: Session, tenant_id: UUID, tenant_name: str, shortlist_size: int = 25
) -> tuple[bytes, dict[str, Any]]:
    """Returns the workbook bytes and the summary figures calculated from the exported rows."""
    leads = db.execute(text(EXPORT_SQL), {"t": tenant_id}).mappings().all()
    channels: dict[UUID, dict[str, str]] = {}
    for ch in (
        db.execute(
            text(
                "SELECT l.id AS lead_id, ch.kind, ch.raw_value, ch.purpose, ch.label FROM contact_channels ch "
                "JOIN leads l ON l.tenant_id = ch.tenant_id AND l.company_id = ch.company_id "
                "WHERE ch.tenant_id = :t ORDER BY ch.position, ch.created_at"
            ),
            {"t": tenant_id},
        )
        .mappings()
        .all()
    ):
        by_kind = channels.setdefault(ch["lead_id"], {})
        existing = by_kind.get(ch["kind"])
        if ch["label"] and ch["purpose"] != "general":
            # Written the way the importer reads it back: "0887 000 003 (emergency: 0884 000 004)".
            labelled = f"({ch['label']}: {ch['raw_value']})"
            by_kind[ch["kind"]] = f"{existing} {labelled}" if existing else labelled
        else:
            by_kind[ch["kind"]] = f"{existing}; {ch['raw_value']}" if existing else ch["raw_value"]

    def contact(row: Any, kind: str, state_key: str) -> str:
        value = channels.get(row["lead_id"], {}).get(kind)
        if value:
            return value
        state = (row["contact_states"] or {}).get(state_key)
        return state["raw"] if state else NOT_FOUND

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "All qualified leads"
    headers = _headers(LEAD_HEADERS, tenant_name)
    for column, header in enumerate(headers, start=1):
        cell = sheet.cell(row=1, column=column)
        _write(cell, header)
        cell.font = Font(bold=True)
    exported: list[dict[str, Any]] = []
    for index, row in enumerate(leads, start=2):
        components = row["components"] or {}
        checked: date | None = row["checked_on"]
        values: list[Any] = [
            row["external_id"],
            row["name"],
            row["business_type"],
            row["city"],
            row["country"],
            row["website_url"] or ((row["contact_states"] or {}).get("website_url") or {}).get("raw", NOT_FOUND),
            row["listing_url"],
            contact(row, "phone", "phone"),
            contact(row, "email", "email"),
            contact(row, "contact_page", "contact_page"),
            contact(row, "social", "other_channel"),
            row["website_status_raw"],
            row["observation"],
            row["evidence_url"],
            row["hypothesis"],
            row["recommended_service_raw"],
            row["fit_explanation"],
            row["proposed_benefit"],
            row["outreach_opening"],
            row["discovery_question"],
            row["channel_recommendation_raw"],
            row["total"],
            row["tier"],
            row["confidence"] if row["confidence"] and row["confidence"] != "unknown" else None,
            checked,
            _status_text(row),
            row["notes"],
            components.get("evidence"),
            components.get("relevance"),
            components.get("value"),
            components.get("reachability"),
            components.get("activity"),
            row["industry_group"],
            row["service_category"],
            _tags_text(row),
        ]
        for column, value in enumerate(values, start=1):
            cell = sheet.cell(row=index, column=column)
            _write(cell, value)
            if isinstance(value, date):
                cell.number_format = "yyyy-mm-dd"
        exported.append({"row": row, "values": values})

    ranked = sorted(
        (e for e in exported if e["row"]["total"] is not None),
        key=lambda e: (-e["row"]["total"], e["row"]["external_id"] or "", str(e["row"]["lead_id"])),
    )[:shortlist_size]
    today = date.today()
    shortlist = workbook.create_sheet(f"Outreach shortlist {today.isoformat()}")
    for column, header in enumerate(_headers(SHORTLIST_HEADERS, tenant_name), start=1):
        cell = shortlist.cell(row=1, column=column)
        _write(cell, header)
        cell.font = Font(bold=True)
    for rank, entry in enumerate(ranked, start=1):
        v = entry["values"]
        picks = [
            rank,
            v[0],
            v[1],
            v[3],
            v[2],
            v[21],
            v[22],
            v[7],
            v[8],
            v[10],
            v[20],
            v[5],
            v[6],
            v[12],
            v[13],
            v[15],
            v[18],
            v[23],
            v[25],
        ]
        for column, value in enumerate(picks, start=1):
            _write(shortlist.cell(row=rank + 1, column=column), value)

    tiers = Counter(e["row"]["tier"] or "unscored" for e in exported)
    confidence = Counter(e["row"]["confidence"] or "unknown" for e in exported)
    summary: dict[str, Any] = {
        "total": len(exported),
        "tiers": {k: tiers.get(k, 0) for k in ("A", "B", "C", "unscored")},
        "confidence": {k: confidence.get(k, 0) for k in ("high", "medium", "low", "unknown")},
        "with_phone": sum(1 for e in exported if e["values"][7] != NOT_FOUND),
        "with_email": sum(1 for e in exported if e["values"][8] != NOT_FOUND),
        "with_website": sum(1 for e in exported if e["row"]["website_url"]),
        "shortlist": len(ranked),
        "shortlist_fillers": sum(1 for e in ranked if e["row"]["tier"] != "A"),
        "overridden_scores": sum(1 for e in exported if e["row"]["score_origin"] == "override"),
        "scores_differing_from_source": sum(
            1
            for e in exported
            if e["row"]["source_total"] is not None and e["row"]["source_total"] != e["row"]["total"]
        ),
    }
    summary_sheet = workbook.create_sheet("Research summary")
    lines: list[tuple[str, Any, str]] = [
        (f"{tenant_name} prospect export, {today.isoformat()}", None, ""),
        ("Headline numbers", "Value", "How it is calculated"),
        ("Total leads", summary["total"], "Rows on 'All qualified leads' in this file."),
        ("Tier A (80–100)", summary["tiers"]["A"], "Current scores, computed from the five components."),
        ("Tier B (60–79)", summary["tiers"]["B"], ""),
        ("Tier C (below 60)", summary["tiers"]["C"], ""),
        ("Unscored", summary["tiers"]["unscored"], "Leads without an assessment are unscored, not zero."),
        ("Leads with a phone", summary["with_phone"], ""),
        ("Leads with an email", summary["with_email"], ""),
        ("Leads with a website recorded", summary["with_website"], ""),
        ("Evidence confidence: high", summary["confidence"]["high"], ""),
        ("Evidence confidence: medium", summary["confidence"]["medium"], ""),
        ("Evidence confidence: low", summary["confidence"]["low"], ""),
        ("Leads on the shortlist", summary["shortlist"], "Top leads by current score; ties broken by Lead ID."),
        (
            "Shortlist entries below Tier A",
            summary["shortlist_fillers"],
            "Shown so fillers are not mistaken for Tier A leads.",
        ),
        ("Scores overridden by a reviewer", summary["overridden_scores"], "The exported score is the current one."),
        ("Scores that differ from the imported source", summary["scores_differing_from_source"], ""),
    ]
    for r, (label, value, note) in enumerate(lines, start=1):
        _write(summary_sheet.cell(row=r, column=1), label)
        _write(summary_sheet.cell(row=r, column=2), value)
        _write(summary_sheet.cell(row=r, column=3), note)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue(), summary
