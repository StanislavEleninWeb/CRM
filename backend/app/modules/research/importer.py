"""Prospect import: interpret rows, build a reviewable preview, then commit.

Nothing reaches CRM tables until an explicit commit. Re-importing the same file
updates research data but never duplicates leads, overwrites reviewed scores, resets
outreach, or removes a contact restriction.
"""

import hashlib
import json
import re
from collections import Counter
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.normalize import domain_of, normalize_email, normalize_phone, normalize_url
from app.modules.research import parsing
from app.modules.research.scoring import Rubric, ScoreError, compute_score
from app.modules.research.workbook import ParsedWorkbook

# (field, pattern on the normalised header). First match wins, so specific patterns come first.
FIELD_PATTERNS: tuple[tuple[str, str], ...] = (
    ("lead_id", r"^lead id$"),
    ("business_name", r"^(business|company) name$|^name$|^company$"),
    ("business_type", r"^industry business type$|^business type$"),
    ("industry_group", r"^industry group$"),
    ("city", r"^city$"),
    ("country", r"^country$"),
    ("website_status", r"^website status$"),
    ("website_url", r"^website( url)?$"),
    ("listing_url", r"google.*(maps|profile)|^maps url$|^listing url$"),
    ("channel", r"^recommended contact channel$"),
    ("other_channel", r"^other .*contact channel$"),
    ("contact_page", r"^contact page"),
    ("phone", r"phone"),
    ("email", r"email"),
    ("observation", r"observed issue|^observation"),
    ("evidence_url", r"^evidence .*url$"),
    ("hypothesis", r"hypothesis"),
    ("service_category", r"^service category$"),
    ("recommended_service", r"^recommended .*service$"),
    ("fit", r"^why "),
    ("benefit", r"benefit"),
    ("opening", r"opening"),
    ("question", r"discovery question"),
    ("score_evidence", r"^score evidence"),
    ("score_relevance", r"^score relevance"),
    ("score_value", r"^score (business )?value"),
    ("score_reachability", r"^score reachability"),
    ("score_activity", r"^score (business )?activity"),
    ("score_total", r"^priority score"),
    ("tier", r"^(priority )?tier"),
    ("confidence", r"confidence"),
    ("date_checked", r"^date checked$"),
    ("outreach_status", r"^outreach status$"),
    ("notes", r"^notes$"),
    ("tags", r"tags$"),
)
FIELDS = tuple(name for name, _ in FIELD_PATTERNS)
SCORE_FIELDS = {
    "evidence": "score_evidence",
    "relevance": "score_relevance",
    "value": "score_value",
    "reachability": "score_reachability",
    "activity": "score_activity",
}
MISSING_TRACKED = ("website_url", "phone", "email", "contact_page", "other_channel")


def _normal(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", header.lower()).strip()


def detect_mapping(headers: list[str]) -> dict[str, str]:
    """Map known fields to the file's column headers. Unmapped columns are kept in the raw row."""
    mapping: dict[str, str] = {}
    remaining = list(headers)
    for field, pattern in FIELD_PATTERNS:
        for header in remaining:
            if re.search(pattern, _normal(header)):
                mapping[field] = header
                remaining.remove(header)
                break
    return mapping


def _issue(severity: str, field: str, message: str) -> dict[str, str]:
    return {"severity": severity, "field": field, "message": message}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def interpret_row(
    row: dict[str, Any], mapping: dict[str, str], rubric: Rubric
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Turn one spreadsheet row into structured data plus validation issues."""

    def get(field: str) -> Any:
        header = mapping.get(field)
        return row.get(header) if header else None

    issues: list[dict[str, str]] = []
    name = parsing.clean(get("business_name"))
    if name is None:
        issues.append(_issue("error", "business_name", "The business name is missing."))
    country = parsing.clean(get("country"))

    # --- website and listing ---
    contact_states: dict[str, dict[str, str]] = {}
    for field in MISSING_TRACKED:
        if field in mapping:
            state = parsing.missing_state(get(field))
            if state:
                contact_states[field] = state
    website_raw = parsing.clean(get("website_url"))
    website = None
    if website_raw and "website_url" not in contact_states:
        website = normalize_url(website_raw)
        if website is None:
            issues.append(
                _issue("warning", "website_url", "The website address is not a valid URL and was not stored.")
            )
    status = parsing.parse_website_status(get("website_status"))
    if status.raw and status.base == "unknown":
        issues.append(
            _issue("info", "website_status", "The website status text was not recognised; it is kept as written.")
        )
    listing_url, listing_type, listing_id = parsing.parse_listing(get("listing_url"))

    # --- channels ---
    channels: list[dict[str, Any]] = []
    for entry in parsing.parse_phones(get("phone")):
        normalized, is_e164 = normalize_phone(entry.raw, country)
        if normalized is None:
            issues.append(
                _issue("warning", "phone", f"A phone entry could not be read and was not stored: {entry.raw[:40]}")
            )
            continue
        channels.append(
            {
                "kind": "phone",
                "raw_value": entry.raw,
                "normalized_value": normalized,
                "normalized_is_e164": is_e164,
                "purpose": entry.purpose,
                "label": entry.label,
            }
        )
    email_text = parsing.clean(get("email"))
    if email_text and "email" not in contact_states and not parsing.parse_emails(email_text):
        issues.append(
            _issue("warning", "email", "The email cell does not contain an email address; nothing was stored.")
        )
    for raw_email in parsing.parse_emails(get("email")):
        normalized_email = normalize_email(raw_email)
        if normalized_email is None:
            issues.append(_issue("warning", "email", "An email address is not valid and was not stored."))
            continue
        channels.append(
            {
                "kind": "email",
                "raw_value": raw_email,
                "normalized_value": normalized_email,
                "normalized_is_e164": False,
                "purpose": "general",
                "label": None,
            }
        )
    contact_page_raw = parsing.clean(get("contact_page"))
    if contact_page_raw and "contact_page" not in contact_states:
        page = normalize_url(contact_page_raw)
        if page:
            channels.append(
                {
                    "kind": "contact_page",
                    "raw_value": contact_page_raw,
                    "normalized_value": page,
                    "normalized_is_e164": False,
                    "purpose": "general",
                    "label": None,
                }
            )
        else:
            issues.append(
                _issue("warning", "contact_page", "The contact page address is not a valid URL and was not stored.")
            )
    for other in parsing.split_values(get("other_channel")):
        channels.append(
            {
                "kind": "social",
                "raw_value": other,
                "normalized_value": other.strip().lower(),
                "normalized_is_e164": False,
                "purpose": "general",
                "label": None,
            }
        )

    # --- score ---
    components: dict[str, int | None] = {}
    component_error = False
    for key, field in SCORE_FIELDS.items():
        try:
            components[key] = parsing.parse_int(get(field)) if field in mapping else None
        except ValueError:
            components[key] = None
            component_error = True
            issues.append(_issue("error", field, "A score component is not a whole number."))
    score: dict[str, Any] | None = None
    if not component_error:
        try:
            computed = compute_score({k: v for k, v in components.items() if k in rubric.keys}, rubric)
        except ScoreError as exc:
            issues.append(_issue("error", "score", str(exc)))
            computed = None
        if computed is not None:
            source_total = None
            try:
                source_total = parsing.parse_int(get("score_total"))
            except ValueError:
                issues.append(
                    _issue("warning", "score_total", "The source total is not a number; the computed total is used.")
                )
            source_tier = parsing.clean(get("tier"))
            if source_total is not None and source_total != computed.total:
                issues.append(
                    _issue(
                        "warning",
                        "score_total",
                        f"Source total {source_total} differs from the computed total {computed.total}.",
                    )
                )
            if source_tier is not None and source_tier.upper() != computed.tier:
                issues.append(
                    _issue(
                        "warning", "tier", f"Source tier {source_tier} differs from the computed tier {computed.tier}."
                    )
                )
            score = {
                "components": computed.components,
                "total": computed.total,
                "tier": computed.tier,
                "source_total": source_total,
                "source_tier": source_tier,
            }
        elif not any(i["field"] == "score" for i in issues):
            issues.append(_issue("info", "score", "No score components: the lead is imported unscored."))

    # --- assessment ---
    confidence_raw = (parsing.clean(get("confidence")) or "").lower()
    confidence = confidence_raw if confidence_raw in ("high", "medium", "low") else "unknown"
    if confidence_raw and confidence == "unknown":
        issues.append(_issue("warning", "confidence", "Confidence must be high, medium or low; stored as unknown."))
    checked_on = None
    try:
        checked_on = parsing.parse_date_only(get("date_checked"))
    except ValueError:
        issues.append(_issue("warning", "date_checked", "The check date could not be read and was not stored."))
    recommendation = parsing.parse_channel_recommendation(get("channel"))
    if recommendation.raw and recommendation.preferred is None:
        issues.append(_issue("info", "channel", "The recommended channel was not recognised; it is kept as written."))
    outreach_raw = parsing.clean(get("outreach_status"))

    data = {
        "external_id": parsing.clean(get("lead_id")),
        "company": {
            "name": name,
            "business_type": parsing.clean(get("business_type")),
            "industry_group": parsing.clean(get("industry_group")),
            "city": parsing.clean(get("city")),
            "country": country,
            "website_url": website,
            "domain": domain_of(website),
        },
        "channels": channels,
        "assessment": {
            "checked_on": checked_on.isoformat() if checked_on else None,
            "confidence": confidence,
            "website_status_raw": status.raw,
            "website_base": status.base,
            "website_availability": status.availability,
            "website_transport": status.transport,
            "website_issue_flags": status.flags,
            "website_match": status.match,
            "listing_url": listing_url,
            "listing_id_type": listing_type,
            "listing_id": listing_id,
            "contact_states": contact_states,
            "preferred_channel": recommendation.preferred,
            "fallback_channels": recommendation.fallbacks,
            "channel_instruction": recommendation.instruction,
            "channel_recommendation_raw": recommendation.raw,
            "service_category": parsing.clean(get("service_category")),
            "recommended_service_raw": parsing.clean(get("recommended_service")),
            "recommended_services": parsing.split_values(get("recommended_service")),
            "fit_explanation": parsing.clean(get("fit")),
            "proposed_benefit": parsing.clean(get("benefit")),
            "outreach_opening": parsing.clean(get("opening")),
            "discovery_question": parsing.clean(get("question")),
            "outreach_status_raw": outreach_raw,
            "notes": parsing.clean(get("notes")),
        },
        "observation": {"text": parsing.clean(get("observation")), "evidence_url": parsing.clean(get("evidence_url"))},
        "hypothesis": parsing.clean(get("hypothesis")),
        "score": score,
        "tags": parsing.split_values(get("tags")),
        "outreach_status": parsing.slug(outreach_raw) if outreach_raw else None,
    }
    return data, issues


def _raw(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _json_safe(value) for key, value in row.items() if key != "__row__"}


def active_rubric(db: Session, tenant_id: UUID) -> tuple[UUID, Rubric]:
    row = (
        db.execute(text("SELECT * FROM rubric_versions WHERE tenant_id = :t AND is_active"), {"t": tenant_id})
        .mappings()
        .one()
    )
    return row["id"], Rubric.from_row(row)


def build_preview(
    db: Session,
    tenant_id: UUID,
    import_id: UUID,
    workbook: ParsedWorkbook,
    mapping_override: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Interpret every row, find duplicates, store the rows for review and return the report."""
    _, rubric = active_rubric(db, tenant_id)
    mapping = detect_mapping(workbook.leads.headers)
    for field, header in (mapping_override or {}).items():
        if field in FIELDS and header in workbook.leads.headers:
            mapping = {f: h for f, h in mapping.items() if h != header}
            mapping[field] = header
    unmapped = [h for h in workbook.leads.headers if h not in mapping.values()]

    existing_by_external = {
        r.external_id: r.id
        for r in db.execute(
            text("SELECT id, external_id FROM leads WHERE tenant_id = :t AND external_id IS NOT NULL"), {"t": tenant_id}
        )
    }
    existing_by_name = {
        (r.name, r.city): r.lead_id
        for r in db.execute(
            text(
                "SELECT lower(c.name) AS name, lower(COALESCE(c.city, '')) AS city, min(l.id::text)::uuid AS lead_id "
                "FROM companies c JOIN leads l ON l.tenant_id = c.tenant_id AND l.company_id = c.id "
                "WHERE c.tenant_id = :t AND c.merged_into_id IS NULL GROUP BY 1, 2"
            ),
            {"t": tenant_id},
        )
    }

    db.execute(
        text("DELETE FROM import_rows WHERE tenant_id = :t AND import_id = :i"), {"t": tenant_id, "i": import_id}
    )
    seen_ids: set[str] = set()
    counts: Counter[str] = Counter()
    tiers: Counter[str] = Counter()
    confidence: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    issue_sample: list[dict[str, Any]] = []
    mismatches = 0
    payload: list[dict[str, Any]] = []
    for row in workbook.leads.rows:
        data, issues = interpret_row(row, mapping, rubric)
        external_id = data["external_id"]
        duplicate_lead_id, duplicate_reason, action = None, None, "create"
        if external_id:
            if external_id in seen_ids:
                issues.append(_issue("error", "lead_id", f"Lead ID {external_id} appears more than once in this file."))
            seen_ids.add(external_id)
            if external_id in existing_by_external:
                duplicate_lead_id, duplicate_reason, action = (
                    existing_by_external[external_id],
                    "same_lead_id",
                    "update",
                )
        else:
            key = ((data["company"]["name"] or "").lower(), (data["company"]["city"] or "").lower())
            if key in existing_by_name:
                duplicate_lead_id, duplicate_reason, action = existing_by_name[key], "same_name_and_city", "skip"
                issues.append(
                    _issue(
                        "warning",
                        "business_name",
                        "A company with this name and city already exists. Review before importing.",
                    )
                )
        has_error = any(i["severity"] == "error" for i in issues)
        if has_error:
            action = "skip"
        counts[action] += 1
        counts["rows"] += 1
        counts["with_errors"] += has_error
        counts["with_warnings"] += any(i["severity"] == "warning" for i in issues)
        if data["score"]:
            tiers[data["score"]["tier"]] += 1
            mismatches += any(i["field"] in ("score_total", "tier") and "differs" in i["message"] for i in issues)
        else:
            tiers["unscored"] += 1
        confidence[data["assessment"]["confidence"]] += 1
        for field, state in data["assessment"]["contact_states"].items():
            if state["state"] == "not_found":
                missing[field] += 1
        for issue in issues:
            if issue["severity"] != "info" and len(issue_sample) < 200:
                issue_sample.append({"row": row["__row__"], "lead_id": external_id, **issue})
        payload.append(
            {
                "t": tenant_id,
                "i": import_id,
                "n": row["__row__"],
                "e": external_id,
                "d": json.dumps(data),
                "r": json.dumps(_raw(row), ensure_ascii=False),
                "s": json.dumps(issues),
                "dl": duplicate_lead_id,
                "dr": duplicate_reason,
                "a": action,
            }
        )
    if payload:
        db.execute(
            text(
                "INSERT INTO import_rows (tenant_id, import_id, row_number, external_id, data, raw, issues, "
                "duplicate_lead_id, duplicate_reason, action) VALUES (:t, :i, :n, :e, CAST(:d AS jsonb), "
                "CAST(:r AS jsonb), CAST(:s AS jsonb), :dl, :dr, :a)"
            ),
            payload,
        )

    shortlist_report: dict[str, Any] | None = None
    shortlist_rows: list[dict[str, Any]] = []
    if workbook.shortlist is not None:
        id_header = next((h for h in workbook.shortlist.headers if _normal(h) == "lead id"), None)
        rank_header = next((h for h in workbook.shortlist.headers if _normal(h) == "rank"), None)
        unresolved: list[str] = []
        for position, row in enumerate(workbook.shortlist.rows, start=1):
            lead_ref = parsing.clean(row.get(id_header)) if id_header else None
            if lead_ref is None:
                continue
            try:
                rank = parsing.parse_int(row.get(rank_header)) if rank_header else None
            except ValueError:
                rank = None
            shortlist_rows.append({"lead_id": lead_ref, "rank": rank or position})
            if lead_ref not in seen_ids:
                unresolved.append(lead_ref)
        shortlist_report = {
            "sheet": workbook.shortlist.name,
            "rows": len(shortlist_rows),
            "unresolved_lead_ids": unresolved,
            "creates_leads": False,
            "formula_cells_without_cache": workbook.shortlist.formula_cells_without_cache,
        }

    report = {
        "kind": workbook.kind,
        "sheet": workbook.leads.name,
        "headers": workbook.leads.headers,
        "mapping": mapping,
        "unmapped_headers": unmapped,
        "counts": dict(counts),
        "tiers": dict(tiers),
        "confidence": dict(confidence),
        "missing_not_found": dict(missing),
        "score_mismatches": mismatches,
        "formula_cells_without_cache": workbook.leads.formula_cells_without_cache,
        "shortlist": shortlist_report,
        "summary_sheet": workbook.summary_sheet_name,
        "ignored_sheets": workbook.ignored_sheets,
        "issues_sample": issue_sample,
    }
    db.execute(
        text(
            "UPDATE imports SET status = 'ready', mapping = CAST(:m AS jsonb), report = CAST(:r AS jsonb), "
            "context = CAST(:c AS jsonb), error = NULL WHERE tenant_id = :t AND id = :i"
        ),
        {
            "m": json.dumps(mapping),
            "r": json.dumps(report, ensure_ascii=False),
            "t": tenant_id,
            "i": import_id,
            "c": json.dumps(
                {
                    "summary": workbook.summary,
                    "summary_sheet": workbook.summary_sheet_name,
                    "shortlist": shortlist_rows,
                    "shortlist_sheet": workbook.shortlist.name if workbook.shortlist else None,
                },
                ensure_ascii=False,
            ),
        },
    )
    return report


# --- commit ----------------------------------------------------------------------------------

COMPANY_FILLABLE = ("business_type", "industry_group", "city", "country", "website_url", "domain")
ASSESSMENT_COLUMNS = (
    "checked_on",
    "confidence",
    "website_status_raw",
    "website_base",
    "website_availability",
    "website_transport",
    "website_issue_flags",
    "website_match",
    "listing_url",
    "listing_id_type",
    "listing_id",
    "preferred_channel",
    "fallback_channels",
    "channel_instruction",
    "channel_recommendation_raw",
    "recommended_service_raw",
    "recommended_services",
    "fit_explanation",
    "proposed_benefit",
    "outreach_opening",
    "discovery_question",
    "outreach_status_raw",
    "notes",
)


def _content_hash(data: dict[str, Any]) -> str:
    relevant = {k: data[k] for k in ("assessment", "observation", "hypothesis")}
    return hashlib.sha256(json.dumps(relevant, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def commit_import(db: Session, tenant_id: UUID, import_id: UUID, actor_id: UUID | None) -> dict[str, Any]:
    """Apply a reviewed import. Runs inside the caller's transaction."""
    rubric_id, _ = active_rubric(db, tenant_id)
    scope = {"t": tenant_id}
    rows = (
        db.execute(
            text("SELECT * FROM import_rows WHERE tenant_id = :t AND import_id = :i ORDER BY row_number"),
            {**scope, "i": import_id},
        )
        .mappings()
        .all()
    )
    result: Counter[str] = Counter()
    lead_by_external: dict[str, UUID] = {}
    services: dict[str, UUID] = {}
    tag_ids: dict[str, UUID] = {}
    latest_check: date | None = None

    def service_id(name: str | None) -> UUID | None:
        if not name:
            return None
        if name not in services:
            services[name] = db.execute(
                text(
                    "INSERT INTO service_catalog (tenant_id, name) VALUES (:t, :n) "
                    "ON CONFLICT (tenant_id, name) DO UPDATE SET name = service_catalog.name RETURNING id"
                ),
                {**scope, "n": name},
            ).scalar_one()
        return services[name]

    def tag_id(name: str) -> UUID:
        if name not in tag_ids:
            tag_ids[name] = db.execute(
                text(
                    "INSERT INTO tags (tenant_id, name) VALUES (:t, :n) "
                    "ON CONFLICT (tenant_id, name) DO UPDATE SET name = tags.name RETURNING id"
                ),
                {**scope, "n": name},
            ).scalar_one()
        return tag_ids[name]

    for row in rows:
        data, action = row["data"], row["action"]
        if action == "skip":
            result["skipped"] += 1
            continue
        company, assessment = data["company"], data["assessment"]
        lead_id: UUID | None = None
        company_id: UUID | None = None
        if data["external_id"]:
            found = db.execute(
                text("SELECT id, company_id FROM leads WHERE tenant_id = :t AND external_id = :e FOR UPDATE"),
                {**scope, "e": data["external_id"]},
            ).one_or_none()
            if found:
                lead_id, company_id = found.id, found.company_id
        elif action == "update" and row["duplicate_lead_id"]:
            found = db.execute(
                text("SELECT id, company_id FROM leads WHERE tenant_id = :t AND id = :l FOR UPDATE"),
                {**scope, "l": row["duplicate_lead_id"]},
            ).one_or_none()
            if found:
                lead_id, company_id = found.id, found.company_id

        created = lead_id is None
        if created:
            company_id = db.execute(
                text(
                    "INSERT INTO companies (tenant_id, name, business_type, industry_group, city, country, website_url, "
                    "domain, source) VALUES (:t, :name, :business_type, :industry_group, :city, :country, :website_url, "
                    ":domain, 'import') RETURNING id"
                ),
                {**scope, **company},
            ).scalar_one()
            lead_id = db.execute(
                text(
                    "INSERT INTO leads (tenant_id, company_id, external_id, status, outreach_status, source) "
                    "VALUES (:t, :c, :e, 'qualified', COALESCE(:o, 'not_contacted'), 'import') RETURNING id"
                ),
                {**scope, "c": company_id, "e": data["external_id"], "o": data["outreach_status"]},
            ).scalar_one()
        else:
            # Fill blanks only: values a user has edited are never overwritten by a re-import.
            db.execute(
                text(
                    "UPDATE companies SET "
                    + ", ".join(f"{c} = COALESCE({c}, :{c})" for c in COMPANY_FILLABLE)
                    + " WHERE tenant_id = :t AND id = :id"
                ),
                {**scope, "id": company_id, **{c: company[c] for c in COMPANY_FILLABLE}},
            )
        assert lead_id is not None and company_id is not None
        if data["external_id"]:
            lead_by_external[data["external_id"]] = lead_id

        # Channels: add what is new. Existing rows, including any restriction, are left untouched.
        for channel in data["channels"]:
            db.execute(
                text(
                    """
                    INSERT INTO contact_channels (tenant_id, company_id, kind, purpose, raw_value, normalized_value,
                        normalized_is_e164, label, source_type, source_date, position)
                    SELECT :t, :c, :kind, :purpose, :raw_value, :normalized_value, :normalized_is_e164, :label,
                           'user_import', :d,
                           (SELECT COALESCE(max(position), 0) + 1 FROM contact_channels WHERE tenant_id = :t AND company_id = :c)
                    WHERE NOT EXISTS (
                        SELECT 1 FROM contact_channels WHERE tenant_id = :t AND company_id = :c AND kind = :kind
                          AND normalized_value = :normalized_value)
                    """
                ),
                {**scope, "c": company_id, "d": assessment["checked_on"], **channel},
            )

        digest = _content_hash(data)
        current_hash = db.execute(
            text("SELECT content_hash FROM lead_assessments WHERE tenant_id = :t AND lead_id = :l AND is_current"),
            {**scope, "l": lead_id},
        ).scalar_one_or_none()
        research_changed = current_hash != digest
        if research_changed:
            db.execute(
                text(
                    "UPDATE lead_assessments SET is_current = false, superseded_at = now() "
                    "WHERE tenant_id = :t AND lead_id = :l AND is_current"
                ),
                {**scope, "l": lead_id},
            )
            db.execute(
                text(
                    "INSERT INTO lead_assessments (tenant_id, lead_id, import_id, service_id, contact_states, raw_row, "
                    "content_hash, " + ", ".join(ASSESSMENT_COLUMNS) + ") VALUES (:t, :l, :i, :service_id, "
                    "CAST(:contact_states AS jsonb), CAST(:raw_row AS jsonb), :hash, "
                    + ", ".join(f":{c}" for c in ASSESSMENT_COLUMNS)
                    + ")"
                ),
                {
                    **scope,
                    "l": lead_id,
                    "i": import_id,
                    "hash": digest,
                    "service_id": service_id(assessment["service_category"]),
                    "contact_states": json.dumps(assessment["contact_states"]),
                    "raw_row": json.dumps(row["raw"], ensure_ascii=False),
                    **{c: assessment[c] for c in ASSESSMENT_COLUMNS},
                },
            )
            for table, value, extra_cols, extra_vals, extra in (
                (
                    "observations",
                    data["observation"]["text"],
                    ", evidence_url, observed_on",
                    ", :url, :d",
                    {"url": data["observation"]["evidence_url"], "d": assessment["checked_on"]},
                ),
                ("hypotheses", data["hypothesis"], "", "", {}),
            ):
                same = (
                    value
                    and db.execute(
                        text(
                            f"SELECT 1 FROM {table} WHERE tenant_id = :t AND lead_id = :l AND superseded_at IS NULL AND text = :x"
                        ),
                        {**scope, "l": lead_id, "x": value},
                    ).first()
                )
                if same:
                    continue
                db.execute(
                    text(
                        f"UPDATE {table} SET superseded_at = now() WHERE tenant_id = :t AND lead_id = :l "
                        "AND superseded_at IS NULL AND source_type = 'user_import'"
                    ),
                    {**scope, "l": lead_id},
                )
                if value:
                    db.execute(
                        text(
                            f"INSERT INTO {table} (tenant_id, lead_id, text, import_id, created_by{extra_cols}) "
                            f"VALUES (:t, :l, :x, :i, :u{extra_vals})"
                        ),
                        {**scope, "l": lead_id, "x": value, "i": import_id, "u": actor_id, **extra},
                    )

        score = data["score"]
        if score:
            current = db.execute(
                text("SELECT components, origin FROM lead_scores WHERE tenant_id = :t AND lead_id = :l AND is_current"),
                {**scope, "l": lead_id},
            ).one_or_none()
            already_recorded = db.execute(
                text(
                    "SELECT 1 FROM lead_scores WHERE tenant_id = :t AND lead_id = :l AND origin = 'import' "
                    "AND components = CAST(:c AS jsonb) AND NOT is_current LIMIT 1"
                ),
                {**scope, "l": lead_id, "c": json.dumps(score["components"])},
            ).first()
            if (current is None or current.components != score["components"]) and not already_recorded:
                # A reviewer's override stays current; the imported score is kept as history beside it.
                keep_override = current is not None and current.origin == "override"
                if current is not None and not keep_override:
                    db.execute(
                        text(
                            "UPDATE lead_scores SET is_current = false WHERE tenant_id = :t AND lead_id = :l AND is_current"
                        ),
                        {**scope, "l": lead_id},
                    )
                db.execute(
                    text(
                        "INSERT INTO lead_scores (tenant_id, lead_id, rubric_version_id, components, total, tier, origin, "
                        "source_total, source_tier, is_current, import_id, created_by) VALUES (:t, :l, :r, "
                        "CAST(:c AS jsonb), :total, :tier, 'import', :st, :stier, :cur, :i, :u)"
                    ),
                    {
                        **scope,
                        "l": lead_id,
                        "r": rubric_id,
                        "c": json.dumps(score["components"]),
                        "total": score["total"],
                        "tier": score["tier"],
                        "st": score["source_total"],
                        "stier": score["source_tier"],
                        "cur": not keep_override,
                        "i": import_id,
                        "u": actor_id,
                    },
                )
                result["scores_recorded"] += 1
                result["override_kept"] += keep_override

        for tag in data["tags"]:
            db.execute(
                text(
                    "INSERT INTO taggings (tenant_id, tag_id, entity_type, entity_id, origin) "
                    "VALUES (:t, :g, 'lead', :l, 'import') ON CONFLICT DO NOTHING"
                ),
                {**scope, "g": tag_id(tag), "l": lead_id},
            )

        if created or research_changed:
            db.execute(
                text(
                    "INSERT INTO activities (tenant_id, kind, summary, actor_user_id, origin, company_id, lead_id, data) "
                    "VALUES (:t, :k, :s, :u, 'import', :c, :l, CAST(:d AS jsonb))"
                ),
                {
                    **scope,
                    "k": "lead.imported" if created else "lead.research_updated",
                    "s": "Imported from a prospect list" if created else "Research updated by a re-import",
                    "u": actor_id,
                    "c": company_id,
                    "l": lead_id,
                    "d": json.dumps({"import_id": str(import_id)}),
                },
            )
        db.execute(
            text("UPDATE import_rows SET lead_id = :l WHERE tenant_id = :t AND id = :id"),
            {**scope, "l": lead_id, "id": row["id"]},
        )
        result["created" if created else ("updated" if research_changed else "unchanged")] += 1
        if assessment["checked_on"]:
            checked = date.fromisoformat(assessment["checked_on"])
            latest_check = checked if latest_check is None or checked > latest_check else latest_check

    shortlist_id = _commit_shortlist(db, tenant_id, import_id, actor_id, lead_by_external, latest_check)
    summary = {**dict(result), "shortlist_id": str(shortlist_id) if shortlist_id else None}
    return summary


def _commit_shortlist(
    db: Session,
    tenant_id: UUID,
    import_id: UUID,
    actor_id: UUID | None,
    lead_by_external: dict[str, UUID],
    latest_check: date | None,
) -> UUID | None:
    """Link the shortlist sheet to existing leads. It never creates leads of its own."""
    context = db.execute(
        text("SELECT context FROM imports WHERE tenant_id = :t AND id = :i"), {"t": tenant_id, "i": import_id}
    ).scalar_one()
    entries = [e for e in context.get("shortlist") or [] if e["lead_id"] in lead_by_external]
    if not entries:
        return None
    sheet = context.get("shortlist_sheet") or "Shortlist"
    base = latest_check or date.today()
    # A sheet called "tomorrow's shortlist" is dated the day after the research was checked.
    shortlist_date = base + timedelta(days=1) if "tomorrow" in sheet.lower() else base
    ordered = [str(lead_by_external[e["lead_id"]]) for e in sorted(entries, key=lambda e: e["rank"])]
    existing = db.execute(
        text(
            "SELECT s.id FROM shortlists s WHERE s.tenant_id = :t AND s.origin = 'import' AND s.shortlist_date = :d "
            "AND (SELECT array_agg(e.lead_id::text ORDER BY e.rank) FROM shortlist_entries e "
            "     WHERE e.tenant_id = s.tenant_id AND e.shortlist_id = s.id) = CAST(:ids AS text[])"
        ),
        {"t": tenant_id, "d": shortlist_date, "ids": ordered},
    ).scalar()
    if existing:
        return existing  # type: ignore[no-any-return]
    shortlist_id: UUID = db.execute(
        text(
            "INSERT INTO shortlists (tenant_id, shortlist_date, name, origin, kind, requested_size, import_id, created_by, note) "
            "VALUES (:t, :d, :n, 'import', 'score_ranked', :size, :i, :u, :note) RETURNING id"
        ),
        {
            "t": tenant_id,
            "d": shortlist_date,
            "n": f"Imported shortlist {shortlist_date.isoformat()}",
            "size": len(entries),
            "i": import_id,
            "u": actor_id,
            "note": f"Source sheet: {sheet}",
        },
    ).scalar_one()
    db.execute(
        text(
            """
            INSERT INTO shortlist_entries (tenant_id, shortlist_id, lead_id, rank, score_at_snapshot, tier_at_snapshot, is_filler)
            SELECT :t, :s, CAST(x.lead_id AS uuid), x.rank, sc.total, sc.tier, COALESCE(sc.tier <> 'A', false)
            FROM unnest(CAST(:ids AS text[])) WITH ORDINALITY AS x (lead_id, rank)
            LEFT JOIN lead_scores sc ON sc.tenant_id = :t AND sc.lead_id = CAST(x.lead_id AS uuid) AND sc.is_current
            """
        ),
        {"t": tenant_id, "s": shortlist_id, "ids": ordered},
    )
    return shortlist_id
