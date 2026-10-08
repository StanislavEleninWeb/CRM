"""Prospect review, the verification queue, daily shortlists and call outcomes."""

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text
from sqlalchemy.engine import RowMapping

from app.core.deps import TenantContext, tenant_with
from app.core.errors import ConflictError
from app.core.normalize import normalize_email
from app.core.pagination import Page, PageParams, page_params
from app.core.time import today_in
from app.modules.crm.common import ValidationFailed, check_owner, execute, log_activity, many, one, scalar, set_clause
from app.modules.crm.companies_router import CHANNEL_COLUMNS, active_restricted_kinds, channel_out
from app.modules.crm.schemas import ChannelOut
from app.modules.identity.audit import record_audit
from app.modules.identity.permissions import Permission
from app.modules.prospects import queries as q
from app.modules.research.router import ScoreOut, _score_rows

router = APIRouter()
Paging = Annotated[PageParams, Depends(page_params)]
READ = tenant_with(Permission.CRM_READ)
REVIEW = tenant_with(Permission.RESEARCH_REVIEW)
CALLS = tenant_with(Permission.CALLS_LOG)

Outcome = Literal[
    "no_answer", "busy", "voicemail", "connected", "follow_up_requested", "wrong_number", "not_interested"
]
OUTREACH_AFTER = {
    "no_answer": "attempted",
    "busy": "attempted",
    "voicemail": "attempted",
    "connected": "contacted",
    "follow_up_requested": "follow_up",
    "wrong_number": None,
    "not_interested": "not_interested",
}
EDITABLE = (
    "outreach_opening",
    "discovery_question",
    "proposed_benefit",
    "fit_explanation",
    "notes",
    "preferred_channel",
    "channel_instruction",
)


class ActionState(BaseModel):
    available: bool
    reason: str | None


class ProspectOut(BaseModel):
    lead_id: UUID
    external_id: str | None
    status: str
    outreach_status: str
    owner_user_id: UUID | None
    next_action: str | None
    next_action_at: datetime | None
    company_id: UUID
    company_name: str
    city: str | None
    country: str | None
    business_type: str | None
    industry_group: str | None
    website_url: str | None
    checked_on: date | None
    is_stale: bool
    confidence: str
    source_type: str | None
    verification_state: str | None
    website_status_raw: str | None
    website_base: str | None
    total: int | None = Field(description="Null means unscored, which is not the same as zero")
    tier: str | None
    components: dict[str, int] | None
    score_origin: str | None
    finding: str | None
    finding_state: str | None
    evidence_url: str | None
    observation_id: UUID | None
    hypothesis: str | None
    hypothesis_status: str | None
    hypothesis_id: UUID | None
    service_category: str | None
    recommended_service_raw: str | None
    fit_explanation: str | None
    proposed_benefit: str | None
    preferred_channel: str | None
    fallback_channels: list[str] | None
    channel_instruction: str | None
    outreach_opening: str | None
    discovery_question: str | None
    notes: str | None
    tags: list[str] | None
    phone_count: int
    dialable_count: int
    email_count: int
    restriction_reasons: str | None
    next_follow_up_at: datetime | None
    open_follow_ups: int
    needs_verification: list[str]
    actions: dict[str, ActionState]
    user_edited_fields: list[str] | None


class CallAttemptOut(BaseModel):
    id: UUID
    lead_id: UUID | None
    company_id: UUID
    channel_id: UUID | None
    dialed_value: str
    launched_at: datetime | None
    outcome: Outcome | None
    outcome_reported_at: datetime | None
    outcome_source: str
    notes: str | None
    follow_up_task_id: UUID | None
    follow_up_scope: str | None
    requested_email: str | None
    created_by: UUID | None
    created_at: datetime
    dial_uri: str | None = None


class ProspectDetail(ProspectOut):
    channels: list[ChannelOut]
    scores: list[ScoreOut]
    calls: list[CallAttemptOut]
    listing_url: str | None
    listing_id_type: str | None


class AssessmentEdit(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}
    outreach_opening: str | None = Field(default=None, max_length=5000)
    discovery_question: str | None = Field(default=None, max_length=2000)
    proposed_benefit: str | None = Field(default=None, max_length=2000)
    fit_explanation: str | None = Field(default=None, max_length=5000)
    notes: str | None = Field(default=None, max_length=10000)
    preferred_channel: Literal["phone", "email", "contact_page", "social", "other"] | None = None
    channel_instruction: str | None = Field(default=None, max_length=500)


class VerifyFinding(BaseModel):
    state: Literal["verified", "contradicted"]
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _note_when_contradicted(self) -> "VerifyFinding":
        if self.state == "contradicted" and not (self.note or "").strip():
            raise ValueError("say what you found instead")
        return self


class ResolveHypothesis(BaseModel):
    status: Literal["confirmed", "rejected"]
    note: str | None = Field(default=None, max_length=2000)


class Dismiss(BaseModel):
    reason: str = Field(min_length=2, max_length=500)


class CallStart(BaseModel):
    channel_id: UUID


class CallOutcome(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}
    outcome: Outcome
    notes: str | None = Field(default=None, max_length=5000)
    follow_up_at: datetime | None = None
    follow_up_note: str | None = Field(default=None, max_length=500)
    follow_up_scope: str | None = Field(
        default=None,
        max_length=500,
        description="What the person asked to be contacted about. Not consent for anything else.",
    )
    follow_up_assignee: UUID | None = None
    requested_email: str | None = Field(default=None, max_length=254)
    do_not_call: bool = False
    do_not_call_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _consistent(self) -> "CallOutcome":
        if self.outcome == "follow_up_requested" and self.follow_up_at is None:
            raise ValueError("say when to follow up")
        if self.do_not_call and not self.do_not_call_reason:
            raise ValueError("give the reason for not calling again")
        if self.requested_email is not None and normalize_email(self.requested_email) is None:
            raise ValueError("enter a valid email address")
        return self


class TenantQueueSettings(BaseModel):
    shortlist_size: int = Field(ge=1, le=500)
    evidence_freshness_days: int = Field(ge=1, le=3650)


class ShortlistOut(BaseModel):
    id: UUID
    shortlist_date: date
    name: str
    origin: str
    kind: str
    requested_size: int
    entry_count: int
    filler_count: int
    note: str | None
    created_at: datetime


class ShortlistEntryOut(BaseModel):
    rank: int
    is_filler: bool
    reason: str | None
    score_at_snapshot: int | None
    tier_at_snapshot: str | None
    prospect: ProspectOut


class ShortlistDetail(ShortlistOut):
    tie_break: str
    entries: list[ShortlistEntryOut]


class GenerateShortlist(BaseModel):
    kind: Literal["call_queue", "score_ranked"] = "call_queue"
    day: Literal["today", "tomorrow"] = "today"
    regenerate: bool = False


class QueueOut(BaseModel):
    queue_date: date
    timezone: str
    requested_size: int
    tie_break: str
    shortfall: int
    shortfall_reason: str | None
    entries: list[ShortlistEntryOut]


# --- helpers ---------------------------------------------------------------------------------


def _settings(ctx: TenantContext) -> RowMapping:
    return one(
        ctx,
        "SELECT timezone, shortlist_size, evidence_freshness_days FROM tenants WHERE id = :tenant_id",
        {},
        "Workspace not found.",
    )


def _base_params(ctx: TenantContext, settings: RowMapping | None = None) -> dict[str, Any]:
    settings = settings or _settings(ctx)
    today = today_in(settings["timezone"])
    return {"stale_before": q.stale_before(today, settings["evidence_freshness_days"])}


def _prospect(ctx: TenantContext, row: Mapping[Any, Any]) -> ProspectOut:
    data = dict(row)
    data.pop("assessment_id", None)
    return ProspectOut(
        **data,
        needs_verification=q.verification_reasons(row),
        actions={
            k: ActionState(**v) for k, v in q.actions(row, can_call=Permission.CALLS_LOG in ctx.permissions).items()
        },
    )


def _one_prospect(ctx: TenantContext, lead_id: UUID) -> RowMapping:
    return one(
        ctx,
        f"{q.PROSPECTS_CTE} SELECT * FROM p WHERE p.lead_id = :lead_id",
        {**_base_params(ctx), "lead_id": lead_id},
        "Prospect not found.",
    )


def _lock_lead(ctx: TenantContext, lead_id: UUID) -> RowMapping:
    return one(
        ctx,
        "SELECT id, company_id, status, outreach_status FROM leads WHERE tenant_id = :tenant_id AND id = :id FOR UPDATE",
        {"id": lead_id},
        "Prospect not found.",
    )


# --- list and detail -------------------------------------------------------------------------


@router.get("/prospects", response_model=Page[ProspectOut], operation_id="listProspects", tags=["prospects"])
def list_prospects(
    paging: Paging,
    ctx: TenantContext = READ,
    search: Annotated[str | None, Query(alias="q", max_length=200)] = None,
    city: Annotated[str | None, Query(max_length=200)] = None,
    country: Annotated[str | None, Query(max_length=200)] = None,
    industry_group: Annotated[str | None, Query(max_length=200)] = None,
    service: Annotated[str | None, Query(max_length=200)] = None,
    tag: Annotated[str | None, Query(max_length=120)] = None,
    tier: Literal["A", "B", "C", "unscored"] | None = None,
    confidence: Literal["high", "medium", "low", "unknown"] | None = None,
    freshness: Literal["fresh", "stale"] | None = None,
    channel: Literal["phone", "email", "contact_page", "social", "other"] | None = None,
    outreach_status: Annotated[str | None, Query(max_length=60)] = None,
    status: Literal["discovered", "needs_review", "qualified", "disqualified", "converted"] | None = None,
    needs_verification: bool | None = None,
    callable_only: bool = False,
    owner_user_id: UUID | None = None,
    sort: Literal["score", "name", "checked_on", "-checked_on", "follow_up"] = "score",
) -> Page[ProspectOut]:
    where: list[str] = ["true"]
    params: dict[str, Any] = _base_params(ctx)
    for column, value in (
        ("city", city),
        ("country", country),
        ("industry_group", industry_group),
        ("service_category", service),
    ):
        if value:
            where.append(f"lower(p.{column}) = lower(:{column})")
            params[column] = value
    for column, exact in (
        ("confidence", confidence),
        ("preferred_channel", channel),
        ("outreach_status", outreach_status),
        ("status", status),
        ("owner_user_id", owner_user_id),
    ):
        if exact is not None:
            where.append(f"p.{column} = :{column}")
            params[column] = exact
    if status is None:
        where.append("p.status <> 'disqualified'")
    if tier:
        where.append("p.tier IS NULL" if tier == "unscored" else "p.tier = :tier")
        params["tier"] = tier
    if freshness:
        where.append("p.is_stale" if freshness == "stale" else "NOT p.is_stale")
    if tag:
        where.append("EXISTS (SELECT 1 FROM unnest(p.tags) AS t(name) WHERE lower(t.name) = lower(:tag))")
        params["tag"] = tag
    if search:
        escaped = search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(lower(p.company_name) LIKE :q OR p.external_id ILIKE :q_exact)")
        params |= {"q": f"%{escaped}%", "q_exact": search}
    if needs_verification is not None:
        where.append(q.NEEDS_VERIFICATION_SQL if needs_verification else f"NOT {q.NEEDS_VERIFICATION_SQL}")
    if callable_only:
        where.append(f"p.dialable_count > 0 AND {q.ACTIVE_PROSPECT_SQL}")
    condition = " AND ".join(where)
    ordering = {
        "score": q.ORDER_BY_SCORE,
        "name": "lower(p.company_name), p.lead_id",
        "checked_on": "p.checked_on ASC NULLS FIRST, p.lead_id",
        "-checked_on": "p.checked_on DESC NULLS LAST, p.lead_id",
        "follow_up": "p.next_follow_up_at ASC NULLS LAST, " + q.ORDER_BY_SCORE,
    }[sort]
    total = scalar(ctx, f"{q.PROSPECTS_CTE} SELECT count(*) FROM p WHERE {condition}", params)
    rows = many(
        ctx,
        f"{q.PROSPECTS_CTE} SELECT * FROM p WHERE {condition} ORDER BY {ordering} LIMIT :limit OFFSET :offset",
        {**params, "limit": paging.limit, "offset": paging.offset},
    )
    return Page(items=[_prospect(ctx, r) for r in rows], total=total, limit=paging.limit, offset=paging.offset)


@router.get("/prospects/facets", operation_id="getProspectFacets", tags=["prospects"])
def prospect_facets(ctx: TenantContext = READ) -> dict[str, list[str]]:
    """Distinct values for the filter controls."""
    params = _base_params(ctx)

    def distinct(column: str) -> list[str]:
        return [
            r["v"]
            for r in many(
                ctx,
                f"{q.PROSPECTS_CTE} SELECT DISTINCT p.{column} AS v FROM p WHERE p.{column} IS NOT NULL ORDER BY 1 LIMIT 300",
                params,
            )
        ]

    tags = many(
        ctx,
        f"{q.PROSPECTS_CTE} SELECT DISTINCT t.name AS v FROM p, unnest(p.tags) AS t(name) ORDER BY 1 LIMIT 300",
        params,
    )
    return {
        "cities": distinct("city"),
        "industry_groups": distinct("industry_group"),
        "services": distinct("service_category"),
        "outreach_statuses": distinct("outreach_status"),
        "tags": [r["v"] for r in tags],
    }


def _calls(ctx: TenantContext, lead_id: UUID) -> list[CallAttemptOut]:
    rows = many(
        ctx,
        "SELECT * FROM call_attempts WHERE tenant_id = :tenant_id AND lead_id = :l ORDER BY created_at DESC, id LIMIT 100",
        {"l": lead_id},
    )
    return [CallAttemptOut(**r) for r in rows]


@router.get("/prospects/{lead_id}", response_model=ProspectDetail, operation_id="getProspect", tags=["prospects"])
def get_prospect(lead_id: UUID, ctx: TenantContext = READ) -> ProspectDetail:
    row = _one_prospect(ctx, lead_id)
    base = _prospect(ctx, row)
    restricted = active_restricted_kinds(ctx, row["company_id"])
    channels = many(
        ctx,
        f"SELECT {CHANNEL_COLUMNS} FROM contact_channels WHERE tenant_id = :tenant_id AND company_id = :c ORDER BY kind, position, created_at",
        {"c": row["company_id"]},
    )
    inactive = not base.actions["call"].available and base.actions["call"].reason in (
        "prospect_inactive",
        "no_permission",
    )
    outs = [channel_out(c, restricted) for c in channels]
    if inactive:
        outs = [c.model_copy(update={"dial_uri": None}) for c in outs]
    return ProspectDetail(
        **base.model_dump(),
        channels=outs,
        scores=_score_rows(ctx, lead_id),
        calls=_calls(ctx, lead_id),
        listing_url=row["listing_url"],
        listing_id_type=row["listing_id_type"],
    )


# --- reviewed edits, verification, dismissal -------------------------------------------------


@router.patch(
    "/prospects/{lead_id}/assessment",
    response_model=ProspectDetail,
    operation_id="editProspectAssessment",
    tags=["prospects"],
)
def edit_assessment(lead_id: UUID, body: AssessmentEdit, ctx: TenantContext = REVIEW) -> ProspectDetail:
    lead = _lock_lead(ctx, lead_id)
    current = one(
        ctx,
        "SELECT * FROM lead_assessments WHERE tenant_id = :tenant_id AND lead_id = :l AND is_current FOR UPDATE",
        {"l": lead_id},
        "This prospect has no research to edit yet.",
    )
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if k in EDITABLE and v != current[k]}
    if changes:
        edited = sorted(set(current["user_edited_fields"]) | set(changes))
        execute(
            ctx,
            f"UPDATE lead_assessments SET {set_clause(changes)}, user_edited_fields = :edited WHERE tenant_id = :tenant_id AND id = :id",
            {**changes, "edited": edited, "id": current["id"]},
        )
        log_activity(
            ctx,
            "prospect.edited",
            "Research text edited: " + ", ".join(sorted(k.replace("_", " ") for k in changes)),
            company_id=lead["company_id"],
            lead_id=lead_id,
            data={"changes": {k: {"from": current[k], "to": v} for k, v in changes.items()}},
        )
    return get_prospect(lead_id, ctx)


@router.post(
    "/observations/{observation_id}/verify",
    response_model=ProspectDetail,
    operation_id="verifyObservation",
    tags=["prospects"],
)
def verify_observation(observation_id: UUID, body: VerifyFinding, ctx: TenantContext = REVIEW) -> ProspectDetail:
    """Record that a person re-checked a finding today. Only this turns imported research into verified research."""
    observation = one(
        ctx,
        "UPDATE observations SET verification_state = :s, verification_note = :n, verified_by = :u, verified_at = now() "
        "WHERE tenant_id = :tenant_id AND id = :id AND superseded_at IS NULL RETURNING lead_id, text",
        {"s": body.state, "n": body.note, "u": ctx.user_id, "id": observation_id},
        "Finding not found.",
    )
    lead = _lock_lead(ctx, observation["lead_id"])
    today = today_in(_settings(ctx)["timezone"])
    pending = scalar(
        ctx,
        "SELECT count(*) FROM observations WHERE tenant_id = :tenant_id AND lead_id = :l AND superseded_at IS NULL AND verification_state <> 'verified'",
        {"l": lead["id"]},
    )
    state = "contradicted" if body.state == "contradicted" else ("verified" if pending == 0 else "unverified")
    execute(
        ctx,
        "UPDATE lead_assessments SET verification_state = :s, verified_by = :u, verified_at = now(), "
        "checked_on = CASE WHEN :s = 'unverified' THEN checked_on ELSE :today END "
        "WHERE tenant_id = :tenant_id AND lead_id = :l AND is_current",
        {"s": state, "u": ctx.user_id, "today": today, "l": lead["id"]},
    )
    log_activity(
        ctx,
        f"finding.{body.state}",
        ("Finding verified" if body.state == "verified" else "Finding contradicted")
        + (f": {body.note}" if body.note else ""),
        company_id=lead["company_id"],
        lead_id=lead["id"],
        data={"observation_id": str(observation_id)},
    )
    return get_prospect(lead["id"], ctx)


@router.post(
    "/hypotheses/{hypothesis_id}/resolve",
    response_model=ProspectDetail,
    operation_id="resolveHypothesis",
    tags=["prospects"],
)
def resolve_hypothesis(hypothesis_id: UUID, body: ResolveHypothesis, ctx: TenantContext = REVIEW) -> ProspectDetail:
    hypothesis = one(
        ctx,
        "UPDATE hypotheses SET status = :s, resolution_note = :n, resolved_by = :u, resolved_at = now() "
        "WHERE tenant_id = :tenant_id AND id = :id AND superseded_at IS NULL RETURNING lead_id",
        {"s": body.status, "n": body.note, "u": ctx.user_id, "id": hypothesis_id},
        "Hypothesis not found.",
    )
    lead = _lock_lead(ctx, hypothesis["lead_id"])
    log_activity(
        ctx,
        f"hypothesis.{body.status}",
        f"Hypothesis {body.status}" + (f": {body.note}" if body.note else ""),
        company_id=lead["company_id"],
        lead_id=lead["id"],
    )
    return get_prospect(lead["id"], ctx)


@router.post(
    "/prospects/{lead_id}/dismiss", response_model=ProspectDetail, operation_id="dismissProspect", tags=["prospects"]
)
def dismiss_prospect(lead_id: UUID, body: Dismiss, ctx: TenantContext = REVIEW) -> ProspectDetail:
    lead = _lock_lead(ctx, lead_id)
    if lead["status"] == "converted":
        raise ConflictError("A converted lead cannot be dismissed.")
    execute(
        ctx,
        "UPDATE leads SET status = 'disqualified', disqualify_reason = :r WHERE tenant_id = :tenant_id AND id = :id",
        {"r": body.reason, "id": lead_id},
    )
    log_activity(
        ctx,
        "prospect.dismissed",
        f"Dismissed: {body.reason}",
        company_id=lead["company_id"],
        lead_id=lead_id,
        data={"previous_status": lead["status"]},
    )
    return get_prospect(lead_id, ctx)


@router.post(
    "/prospects/{lead_id}/restore", response_model=ProspectDetail, operation_id="restoreProspect", tags=["prospects"]
)
def restore_prospect(lead_id: UUID, ctx: TenantContext = REVIEW) -> ProspectDetail:
    """Undo a dismissal."""
    lead = _lock_lead(ctx, lead_id)
    if lead["status"] != "disqualified":
        raise ConflictError("This prospect is not dismissed.")
    execute(
        ctx,
        "UPDATE leads SET status = 'qualified', disqualify_reason = NULL WHERE tenant_id = :tenant_id AND id = :id",
        {"id": lead_id},
    )
    log_activity(ctx, "prospect.restored", "Dismissal undone", company_id=lead["company_id"], lead_id=lead_id)
    return get_prospect(lead_id, ctx)


@router.patch(
    "/prospects/{lead_id}/owner", response_model=ProspectDetail, operation_id="assignProspect", tags=["prospects"]
)
def assign_prospect(
    lead_id: UUID, owner_user_id: UUID | None = None, ctx: TenantContext = tenant_with(Permission.CRM_WRITE)
) -> ProspectDetail:
    _lock_lead(ctx, lead_id)
    check_owner(ctx, owner_user_id)
    execute(
        ctx,
        "UPDATE leads SET owner_user_id = :o WHERE tenant_id = :tenant_id AND id = :id",
        {"o": owner_user_id, "id": lead_id},
    )
    return get_prospect(lead_id, ctx)


# --- calls -----------------------------------------------------------------------------------


@router.post(
    "/prospects/{lead_id}/calls",
    response_model=CallAttemptOut,
    status_code=201,
    operation_id="startCall",
    tags=["calls"],
)
def start_call(lead_id: UUID, body: CallStart, ctx: TenantContext = CALLS) -> CallAttemptOut:
    """Record that the dialler is being opened for a permitted number. This is not a completed call."""
    lead = _lock_lead(ctx, lead_id)
    row = _one_prospect(ctx, lead_id)
    channel = one(
        ctx,
        f"SELECT {CHANNEL_COLUMNS} FROM contact_channels WHERE tenant_id = :tenant_id AND id = :c AND company_id = :co AND kind = 'phone'",
        {"c": body.channel_id, "co": lead["company_id"]},
        "That phone number does not belong to this prospect.",
    )
    usable = channel_out(channel, active_restricted_kinds(ctx, lead["company_id"]))
    availability = q.actions(row, can_call=True)["call"]
    if not availability["available"] and availability["reason"] != "no_usable_number":
        raise ConflictError(f"This prospect cannot be called: {str(availability['reason']).replace('_', ' ')}.")
    if usable.dial_uri is None:
        reason = (
            "it is an emergency line" if channel["purpose"] == "emergency" else "it is marked do-not-call or invalid"
        )
        raise ConflictError(f"This number cannot be used for a sales call: {reason}.")
    created = one(
        ctx,
        "INSERT INTO call_attempts (tenant_id, lead_id, company_id, contact_id, channel_id, dialed_value, launched_at, created_by) "
        "VALUES (:tenant_id, :l, :co, :contact, :ch, :v, now(), :u) RETURNING *",
        {
            "l": lead_id,
            "co": lead["company_id"],
            "contact": channel["contact_id"],
            "ch": body.channel_id,
            "v": channel["raw_value"],
            "u": ctx.user_id,
        },
        "Call not found.",
    )
    return CallAttemptOut(**created, dial_uri=usable.dial_uri)


def _apply_outcome(ctx: TenantContext, attempt: RowMapping, body: CallOutcome) -> CallAttemptOut:
    lead_id, company_id = attempt["lead_id"], attempt["company_id"]
    task_id = None
    if body.follow_up_at is not None:
        assignee = body.follow_up_assignee or ctx.user_id
        check_owner(ctx, assignee)
        task_id = scalar(
            ctx,
            "INSERT INTO tasks (tenant_id, title, description, kind, due_at, assignee_user_id, created_by, company_id, lead_id, contact_id) "
            "VALUES (:tenant_id, :title, :d, 'call', :due, :a, :u, :co, :l, :contact) RETURNING id",
            {
                "title": body.follow_up_note or "Call back",
                "d": body.follow_up_scope,
                "due": body.follow_up_at,
                "a": assignee,
                "u": ctx.user_id,
                "co": company_id,
                "l": lead_id,
                "contact": attempt["contact_id"],
            },
        )
    email = normalize_email(body.requested_email) if body.requested_email else None
    if email:
        # A business email given during a call. It records a request for this follow-up, not marketing consent.
        execute(
            ctx,
            """
            INSERT INTO contact_channels (tenant_id, company_id, contact_id, kind, raw_value, normalized_value, label,
                                          source_type, source_date, position)
            SELECT :tenant_id, :co, :contact, 'email', :raw, :n, 'Given by phone for a requested follow-up',
                   'manual_entry', :today,
                   (SELECT COALESCE(max(position), 0) + 1 FROM contact_channels WHERE tenant_id = :tenant_id AND company_id = :co)
            WHERE NOT EXISTS (SELECT 1 FROM contact_channels WHERE tenant_id = :tenant_id AND company_id = :co
                              AND kind = 'email' AND normalized_value = :n)
            """,
            {
                "co": company_id,
                "contact": attempt["contact_id"],
                "raw": body.requested_email,
                "n": email,
                "today": today_in(_settings(ctx)["timezone"]),
            },
        )
    updated = one(
        ctx,
        "UPDATE call_attempts SET outcome = :o, outcome_reported_at = now(), notes = :notes, follow_up_task_id = :task, "
        "follow_up_requested_via = CASE WHEN CAST(:task AS uuid) IS NULL THEN NULL ELSE 'phone call' END, "
        "follow_up_scope = :scope, requested_email = :email WHERE tenant_id = :tenant_id AND id = :id RETURNING *",
        {
            "o": body.outcome,
            "notes": body.notes,
            "task": task_id,
            "scope": body.follow_up_scope,
            "email": email,
            "id": attempt["id"],
        },
        "Call not found.",
    )
    if body.outcome == "wrong_number" and attempt["channel_id"]:
        execute(
            ctx,
            "UPDATE contact_channels SET verification_state = 'invalid' WHERE tenant_id = :tenant_id AND id = :c",
            {"c": attempt["channel_id"]},
        )
    if body.do_not_call:
        execute(
            ctx,
            "INSERT INTO contact_restrictions (tenant_id, company_id, contact_id, channel_kind, reason, source, created_by) "
            "VALUES (:tenant_id, :co, :contact, 'phone', :r, 'call_outcome', :u)",
            {"co": company_id, "contact": attempt["contact_id"], "r": body.do_not_call_reason, "u": ctx.user_id},
        )
        record_audit(
            ctx.db,
            tenant_id=ctx.tenant_id,
            actor_id=ctx.user_id,
            action="restriction.created",
            target_type="company",
            target_id=str(company_id),
            data={"channel_kind": "phone", "reason": body.do_not_call_reason, "source": "call_outcome"},
        )
    status = OUTREACH_AFTER[body.outcome]
    if lead_id and status:
        execute(
            ctx,
            "UPDATE leads SET outreach_status = :s WHERE tenant_id = :tenant_id AND id = :l AND status <> 'converted'",
            {"s": status, "l": lead_id},
        )
    summary = f"Call reported: {body.outcome.replace('_', ' ')}" + (f" — {body.notes[:200]}" if body.notes else "")
    log_activity(
        ctx,
        "call.reported",
        summary,
        company_id=company_id,
        lead_id=lead_id,
        contact_id=attempt["contact_id"],
        data={
            "outcome": body.outcome,
            "dialler_opened": attempt["launched_at"] is not None,
            "follow_up_at": body.follow_up_at,
            "source": "user_reported",
        },
    )
    return CallAttemptOut(**updated)


@router.post(
    "/calls/{call_id}/outcome", response_model=CallAttemptOut, operation_id="reportCallOutcome", tags=["calls"]
)
def report_outcome(call_id: UUID, body: CallOutcome, ctx: TenantContext = CALLS) -> CallAttemptOut:
    """The user says what happened. Until this is called, nothing claims the call connected."""
    attempt = one(
        ctx,
        "SELECT * FROM call_attempts WHERE tenant_id = :tenant_id AND id = :id FOR UPDATE",
        {"id": call_id},
        "Call not found.",
    )
    if attempt["outcome"] is not None:
        raise ConflictError("An outcome was already reported for this call.")
    return _apply_outcome(ctx, attempt, body)


class ManualCall(CallOutcome):
    channel_id: UUID | None = None
    dialed_value: str | None = Field(default=None, max_length=100)


@router.post(
    "/prospects/{lead_id}/calls/manual",
    response_model=CallAttemptOut,
    status_code=201,
    operation_id="logManualCall",
    tags=["calls"],
)
def log_manual_call(lead_id: UUID, body: ManualCall, ctx: TenantContext = CALLS) -> CallAttemptOut:
    """Log a call made outside the app, for example from a desk phone. No dialler launch is recorded."""
    lead = _lock_lead(ctx, lead_id)
    dialed, contact_id = body.dialed_value, None
    if body.channel_id:
        channel = one(
            ctx,
            "SELECT raw_value, contact_id FROM contact_channels WHERE tenant_id = :tenant_id AND id = :c AND company_id = :co AND kind = 'phone'",
            {"c": body.channel_id, "co": lead["company_id"]},
            "That phone number does not belong to this prospect.",
        )
        dialed, contact_id = channel["raw_value"], channel["contact_id"]
    if not dialed:
        raise ValidationFailed("Say which number was called.")
    attempt = one(
        ctx,
        "INSERT INTO call_attempts (tenant_id, lead_id, company_id, contact_id, channel_id, dialed_value, outcome, outcome_reported_at, created_by) "
        "VALUES (:tenant_id, :l, :co, :contact, :ch, :v, 'no_answer', now(), :u) RETURNING *",
        {
            "l": lead_id,
            "co": lead["company_id"],
            "contact": contact_id,
            "ch": body.channel_id,
            "v": dialed,
            "u": ctx.user_id,
        },
        "Call not found.",
    )
    return _apply_outcome(ctx, attempt, CallOutcome(**body.model_dump(exclude={"channel_id", "dialed_value"})))


# --- queue settings, live queue and snapshots ------------------------------------------------


@router.get("/queue-settings", response_model=TenantQueueSettings, operation_id="getQueueSettings", tags=["queue"])
def get_queue_settings(ctx: TenantContext = READ) -> TenantQueueSettings:
    s = _settings(ctx)
    return TenantQueueSettings(shortlist_size=s["shortlist_size"], evidence_freshness_days=s["evidence_freshness_days"])


@router.put("/queue-settings", response_model=TenantQueueSettings, operation_id="updateQueueSettings", tags=["queue"])
def update_queue_settings(
    body: TenantQueueSettings, ctx: TenantContext = tenant_with(Permission.TENANT_SETTINGS)
) -> TenantQueueSettings:
    execute(
        ctx,
        "UPDATE tenants SET shortlist_size = :shortlist_size, evidence_freshness_days = :evidence_freshness_days WHERE id = :tenant_id",
        body.model_dump(),
    )
    return body


def _queue_rows(
    ctx: TenantContext, kind: str, queue_date: date, settings: RowMapping
) -> tuple[list[tuple[RowMapping, str]], int]:
    """Select and order the queue for a date. Returns ``(rows with reason, eligible count)``."""
    tz = ZoneInfo(settings["timezone"])
    end_of_day = datetime.combine(queue_date + timedelta(days=1), time.min, tzinfo=tz)
    params = {**_base_params(ctx, settings), "end_of_day": end_of_day, "limit": settings["shortlist_size"]}
    if kind == "score_ranked":
        condition = f"p.total IS NOT NULL AND {q.ACTIVE_PROSPECT_SQL}"
        rows = many(
            ctx, f"{q.PROSPECTS_CTE} SELECT * FROM p WHERE {condition} ORDER BY {q.ORDER_BY_SCORE} LIMIT :limit", params
        )
        eligible = scalar(ctx, f"{q.PROSPECTS_CTE} SELECT count(*) FROM p WHERE {condition}", params)
        return [(r, "ranked_by_score") for r in rows], eligible
    # Call queue: due follow-ups first, then phone-first prospects, then everyone else who can be called.
    condition = f"p.dialable_count > 0 AND {q.ACTIVE_PROSPECT_SQL}"
    bucket = """
        CASE WHEN p.next_follow_up_at IS NOT NULL AND p.next_follow_up_at < :end_of_day THEN 0
             WHEN p.preferred_channel = 'phone' THEN 1 ELSE 2 END
    """
    rows = many(
        ctx,
        f"{q.PROSPECTS_CTE} SELECT p.*, {bucket} AS bucket FROM p WHERE {condition} "
        f"ORDER BY bucket, CASE WHEN {bucket} = 0 THEN p.next_follow_up_at END, {q.ORDER_BY_SCORE} LIMIT :limit",
        params,
    )
    eligible = scalar(ctx, f"{q.PROSPECTS_CTE} SELECT count(*) FROM p WHERE {condition}", params)
    reasons = {0: "follow_up_due", 1: "phone_first", 2: "callable"}
    return [(r, reasons[r["bucket"]]) for r in rows], eligible


def _entries(ctx: TenantContext, rows: list[tuple[RowMapping, str]]) -> list[ShortlistEntryOut]:
    entries = []
    for rank, (row, reason) in enumerate(rows, start=1):
        data = {k: v for k, v in dict(row).items() if k != "bucket"}
        entries.append(
            ShortlistEntryOut(
                rank=rank,
                is_filler=row["tier"] != "A",
                reason=reason,
                score_at_snapshot=row["total"],
                tier_at_snapshot=row["tier"],
                prospect=_prospect(ctx, data),
            )
        )
    return entries


def _shortfall(requested: int, got: int, eligible: int, kind: str) -> tuple[int, str | None]:
    if got >= requested:
        return 0, None
    what = "prospects with a number that may be called" if kind == "call_queue" else "scored, active prospects"
    return requested - got, f"Only {eligible} {what} are available. The list is not padded."


@router.get("/call-queue", response_model=QueueOut, operation_id="getCallQueue", tags=["queue"])
def call_queue(ctx: TenantContext = READ, day: Literal["today", "tomorrow"] = "today") -> QueueOut:
    """The live call list for a date in the workspace's time zone."""
    settings = _settings(ctx)
    queue_date = today_in(settings["timezone"]) + timedelta(days=1 if day == "tomorrow" else 0)
    rows, eligible = _queue_rows(ctx, "call_queue", queue_date, settings)
    shortfall, reason = _shortfall(settings["shortlist_size"], len(rows), eligible, "call_queue")
    return QueueOut(
        queue_date=queue_date,
        timezone=settings["timezone"],
        requested_size=settings["shortlist_size"],
        tie_break=q.TIE_BREAK,
        shortfall=shortfall,
        shortfall_reason=reason,
        entries=_entries(ctx, rows),
    )


SHORTLIST_SQL = """
    SELECT s.*, (SELECT count(*) FROM shortlist_entries e WHERE e.tenant_id = s.tenant_id AND e.shortlist_id = s.id) AS entry_count,
           (SELECT count(*) FROM shortlist_entries e WHERE e.tenant_id = s.tenant_id AND e.shortlist_id = s.id AND e.is_filler) AS filler_count
    FROM shortlists s WHERE s.tenant_id = :tenant_id
"""


@router.get("/shortlists", response_model=Page[ShortlistOut], operation_id="listShortlists", tags=["queue"])
def list_shortlists(paging: Paging, ctx: TenantContext = READ) -> Page[ShortlistOut]:
    total = scalar(ctx, "SELECT count(*) FROM shortlists WHERE tenant_id = :tenant_id")
    rows = many(
        ctx,
        f"{SHORTLIST_SQL} ORDER BY s.shortlist_date DESC, s.created_at DESC, s.id LIMIT :limit OFFSET :offset",
        {"limit": paging.limit, "offset": paging.offset},
    )
    return Page(items=[ShortlistOut(**r) for r in rows], total=total, limit=paging.limit, offset=paging.offset)


@router.get("/shortlists/{shortlist_id}", response_model=ShortlistDetail, operation_id="getShortlist", tags=["queue"])
def get_shortlist(shortlist_id: UUID, ctx: TenantContext = READ) -> ShortlistDetail:
    """A dated snapshot. Each entry shows the score when it was taken and the prospect as it is now."""
    head = one(ctx, f"{SHORTLIST_SQL} AND s.id = :id", {"id": shortlist_id}, "Shortlist not found.")
    rows = many(
        ctx,
        f"{q.PROSPECTS_CTE} SELECT p.*, e.rank AS e_rank, e.is_filler AS e_filler, e.reason AS e_reason, "
        "e.score_at_snapshot AS e_score, e.tier_at_snapshot AS e_tier FROM shortlist_entries e "
        "JOIN p ON p.lead_id = e.lead_id WHERE e.tenant_id = :tenant_id AND e.shortlist_id = :id ORDER BY e.rank",
        {**_base_params(ctx), "id": shortlist_id},
    )
    entries = []
    for row in rows:
        data = {k: v for k, v in dict(row).items() if not k.startswith("e_")}
        entries.append(
            ShortlistEntryOut(
                rank=row["e_rank"],
                is_filler=row["e_filler"],
                reason=row["e_reason"],
                score_at_snapshot=row["e_score"],
                tier_at_snapshot=row["e_tier"],
                prospect=_prospect(ctx, data),
            )
        )
    return ShortlistDetail(**head, tie_break=q.TIE_BREAK, entries=entries)


@router.post(
    "/shortlists", response_model=ShortlistDetail, status_code=201, operation_id="generateShortlist", tags=["queue"]
)
def generate_shortlist(body: GenerateShortlist, ctx: TenantContext = REVIEW) -> ShortlistDetail:
    """Freeze today's (or tomorrow's) list as a dated snapshot. Fewer entries are returned when fewer are eligible."""
    settings = _settings(ctx)
    queue_date = today_in(settings["timezone"]) + timedelta(days=1 if body.day == "tomorrow" else 0)
    existing = scalar(
        ctx,
        "SELECT id FROM shortlists WHERE tenant_id = :tenant_id AND origin = 'generated' AND kind = :k AND shortlist_date = :d "
        "ORDER BY created_at DESC LIMIT 1",
        {"k": body.kind, "d": queue_date},
    )
    if existing and not body.regenerate:
        return get_shortlist(existing, ctx)
    rows, eligible = _queue_rows(ctx, body.kind, queue_date, settings)
    _, reason = _shortfall(settings["shortlist_size"], len(rows), eligible, body.kind)
    label = "Call queue" if body.kind == "call_queue" else "Score-ranked shortlist"
    shortlist_id = scalar(
        ctx,
        "INSERT INTO shortlists (tenant_id, shortlist_date, name, origin, kind, requested_size, created_by, note) "
        "VALUES (:tenant_id, :d, :n, 'generated', :k, :size, :u, :note) RETURNING id",
        {
            "d": queue_date,
            "n": f"{label} {queue_date.isoformat()}",
            "k": body.kind,
            "size": settings["shortlist_size"],
            "u": ctx.user_id,
            "note": reason,
        },
    )
    if rows:
        ctx.db.execute(
            text(
                "INSERT INTO shortlist_entries (tenant_id, shortlist_id, lead_id, rank, score_at_snapshot, tier_at_snapshot, is_filler, reason) "
                "VALUES (:t, :s, :l, :rank, :score, :tier, :filler, :reason)"
            ),
            [
                {
                    "t": ctx.tenant_id,
                    "s": shortlist_id,
                    "l": row["lead_id"],
                    "rank": rank,
                    "score": row["total"],
                    "tier": row["tier"],
                    "filler": row["tier"] != "A",
                    "reason": why,
                }
                for rank, (row, why) in enumerate(rows, start=1)
            ],
        )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="shortlist.generated",
        target_type="shortlist",
        target_id=str(shortlist_id),
        data={"kind": body.kind, "date": queue_date.isoformat(), "entries": len(rows)},
    )
    return get_shortlist(shortlist_id, ctx)
