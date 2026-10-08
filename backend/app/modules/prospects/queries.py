"""The prospect view: one row per lead with its current research, score and contact options."""

from datetime import date, timedelta
from typing import Any

# Ranking is deterministic: score, then confidence, then freshest check, then Lead ID.
TIE_BREAK = "score (high to low), confidence (high, medium, low), most recently checked, Lead ID"
ORDER_BY_SCORE = """
    p.total DESC NULLS LAST,
    CASE p.confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END,
    p.checked_on DESC NULLS LAST,
    p.external_id NULLS LAST,
    p.lead_id
"""
INACTIVE_OUTREACH = ("not_interested", "do_not_contact")

PROSPECTS_CTE = """
WITH restricted AS (
    SELECT company_id,
           bool_or(channel_kind IN ('phone', 'any')) AS phone_blocked,
           bool_or(channel_kind IN ('email', 'any')) AS email_blocked,
           string_agg(DISTINCT reason, '; ') AS reasons
    FROM contact_restrictions
    WHERE tenant_id = :tenant_id AND lifted_at IS NULL
    GROUP BY company_id
),
phones AS (
    SELECT ch.company_id,
           count(*) AS phone_count,
           count(*) FILTER (WHERE ch.allow_sales_use AND ch.verification_state <> 'invalid') AS dialable_count
    FROM contact_channels ch
    WHERE ch.tenant_id = :tenant_id AND ch.kind = 'phone'
    GROUP BY ch.company_id
),
emails AS (
    SELECT company_id, count(*) AS email_count
    FROM contact_channels WHERE tenant_id = :tenant_id AND kind = 'email' AND NOT do_not_contact
    GROUP BY company_id
),
follow_ups AS (
    SELECT lead_id, min(due_at) AS next_follow_up_at, count(*) AS open_follow_ups
    FROM tasks
    WHERE tenant_id = :tenant_id AND status = 'open' AND kind IN ('call', 'follow_up') AND lead_id IS NOT NULL
    GROUP BY lead_id
),
reported AS (
    -- A wrong number says nothing about the prospect, so it does not count as having reached them.
    SELECT lead_id, max(outcome_reported_at) AS last_outcome_at
    FROM call_attempts
    WHERE tenant_id = :tenant_id AND outcome IS NOT NULL AND outcome <> 'wrong_number' AND lead_id IS NOT NULL
    GROUP BY lead_id
),
p AS (
    SELECT l.id AS lead_id, l.external_id, l.status, l.outreach_status, l.owner_user_id, l.next_action,
           l.next_action_at, l.company_id, l.disqualify_reason,
           c.name AS company_name, c.city, c.country, c.business_type, c.industry_group, c.website_url,
           a.id AS assessment_id, a.checked_on, COALESCE(a.confidence, 'unknown') AS confidence,
           a.source_type, a.verification_state, a.website_status_raw, a.website_base, a.preferred_channel,
           a.fallback_channels, a.channel_instruction, a.recommended_service_raw, a.fit_explanation,
           a.proposed_benefit, a.outreach_opening, a.discovery_question, a.notes, a.listing_url, a.listing_id_type,
           a.user_edited_fields, sv.name AS service_category,
           s.total, s.tier, s.components, s.origin AS score_origin,
           o.id AS observation_id, o.text AS finding, o.evidence_url, o.verification_state AS finding_state,
           h.id AS hypothesis_id, h.text AS hypothesis, h.status AS hypothesis_status,
           COALESCE(ph.phone_count, 0) AS phone_count,
           CASE WHEN COALESCE(r.phone_blocked, false) THEN 0 ELSE COALESCE(ph.dialable_count, 0) END AS dialable_count,
           CASE WHEN COALESCE(r.email_blocked, false) THEN 0 ELSE COALESCE(e.email_count, 0) END AS email_count,
           COALESCE(r.phone_blocked, false) AS phone_blocked, COALESCE(r.email_blocked, false) AS email_blocked,
           r.reasons AS restriction_reasons,
           f.next_follow_up_at, COALESCE(f.open_follow_ups, 0) AS open_follow_ups, rep.last_outcome_at,
           (a.checked_on IS NULL OR a.checked_on < :stale_before) AS is_stale,
           (SELECT array_agg(t.name::text ORDER BY t.name::text) FROM taggings tg
            JOIN tags t ON t.tenant_id = tg.tenant_id AND t.id = tg.tag_id
            WHERE tg.tenant_id = l.tenant_id AND tg.entity_type = 'lead' AND tg.entity_id = l.id) AS tags
    FROM leads l
    JOIN companies c ON c.tenant_id = l.tenant_id AND c.id = l.company_id
    LEFT JOIN lead_assessments a ON a.tenant_id = l.tenant_id AND a.lead_id = l.id AND a.is_current
    LEFT JOIN service_catalog sv ON sv.tenant_id = l.tenant_id AND sv.id = a.service_id
    LEFT JOIN lead_scores s ON s.tenant_id = l.tenant_id AND s.lead_id = l.id AND s.is_current
    LEFT JOIN LATERAL (
        SELECT id, text, evidence_url, verification_state FROM observations
        WHERE tenant_id = l.tenant_id AND lead_id = l.id AND superseded_at IS NULL
        ORDER BY created_at, id LIMIT 1) o ON true
    LEFT JOIN LATERAL (
        SELECT id, text, status FROM hypotheses
        WHERE tenant_id = l.tenant_id AND lead_id = l.id AND superseded_at IS NULL
        ORDER BY created_at, id LIMIT 1) h ON true
    LEFT JOIN restricted r ON r.company_id = l.company_id
    LEFT JOIN phones ph ON ph.company_id = l.company_id
    LEFT JOIN emails e ON e.company_id = l.company_id
    LEFT JOIN follow_ups f ON f.lead_id = l.id
    LEFT JOIN reported rep ON rep.lead_id = l.id
    WHERE l.tenant_id = :tenant_id AND c.merged_into_id IS NULL AND c.archived_at IS NULL
)
"""


def stale_before(today: date, freshness_days: int) -> date:
    return today - timedelta(days=freshness_days)


def verification_reasons(row: Any) -> list[str]:
    """Why a prospect should be checked before fact-specific outreach. Empty means nothing is pending."""
    reasons: list[str] = []
    if row["assessment_id"] is None:
        return ["no_assessment"]
    if row["confidence"] in ("low", "unknown"):
        reasons.append("low_confidence")
    if row["is_stale"]:
        reasons.append("stale_evidence")
    if row["finding_state"] == "contradicted":
        reasons.append("finding_contradicted")
    elif row["finding"] and row["finding_state"] != "verified" and row["tier"] == "A":
        reasons.append("unverified_high_priority")
    return reasons


NEEDS_VERIFICATION_SQL = """(
    p.assessment_id IS NULL OR p.confidence IN ('low', 'unknown') OR p.is_stale
    OR p.finding_state = 'contradicted'
    OR (p.finding IS NOT NULL AND p.finding_state <> 'verified' AND p.tier = 'A')
)"""

ACTIVE_PROSPECT_SQL = (
    "p.status NOT IN ('disqualified', 'converted') AND p.outreach_status NOT IN ('not_interested', 'do_not_contact')"
)


def actions(row: Any, *, can_call: bool, mailbox_connected: bool = False) -> dict[str, dict[str, Any]]:
    """Which actions are available right now, and the reason when one is not."""
    inactive = row["status"] in ("disqualified", "converted") or row["outreach_status"] in INACTIVE_OUTREACH

    def call() -> tuple[bool, str | None]:
        if not can_call:
            return False, "no_permission"
        if inactive:
            return False, "prospect_inactive"
        if row["phone_blocked"]:
            return False, "do_not_call"
        if row["phone_count"] == 0:
            return False, "no_phone_found"
        if row["dialable_count"] == 0:
            return False, "no_usable_number"
        return True, None

    def email() -> tuple[bool, str | None]:
        if inactive:
            return False, "prospect_inactive"
        if row["email_blocked"]:
            return False, "do_not_email"
        if row["email_count"] == 0:
            return False, "no_email_found"
        if not mailbox_connected:
            return False, "mailbox_not_connected"
        return True, None

    call_ok, call_reason = call()
    email_ok, email_reason = email()
    return {
        "call": {"available": call_ok, "reason": call_reason},
        "email": {"available": email_ok, "reason": email_reason},
    }


# How the call queue moves on during and across calling days.
QUEUE_RULES = (
    "A follow-up that is due comes first, whatever happened before.",
    "A prospect with a follow-up booked for a later day waits until that day.",
    "A prospect whose call outcome was reported today is not offered again today.",
    "A prospect you have spoken to (connected) leaves the cold-call queue; continue through its follow-up or opportunity.",
    "Unanswered calls (no answer, busy, voicemail) return on a later day.",
    "After a wrong number the prospect stays in the queue if another number may be called.",
)
# Applies to prospects that are not due for a follow-up. Needs :start_of_day and :end_of_day.
QUEUE_NOT_YET_HANDLED_SQL = """(
    (p.next_follow_up_at IS NULL OR p.next_follow_up_at < :end_of_day)
    AND (p.last_outcome_at IS NULL OR p.last_outcome_at < :start_of_day)
    AND p.outreach_status <> 'contacted'
)"""
FOLLOW_UP_DUE_SQL = "(p.next_follow_up_at IS NOT NULL AND p.next_follow_up_at < :end_of_day)"
