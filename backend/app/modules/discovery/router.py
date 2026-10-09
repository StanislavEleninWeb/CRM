"""Research configurations, runs, the candidate review queue and source policies."""

import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.deps import TenantContext, tenant_with
from app.core.errors import ConflictError
from app.core.normalize import domain_of, normalize_email, normalize_phone
from app.core.pagination import Page, PageParams, page_params
from app.core.time import today_in
from app.modules.crm.common import ValidationFailed, execute, exists, log_activity, many, one, scalar
from app.modules.discovery import orchestrator
from app.modules.identity.audit import record_audit
from app.modules.identity.permissions import Permission
from app.modules.research.importer import active_rubric
from app.modules.research.scoring import ScoreError, compute_score
from app.worker.due import schedule

router = APIRouter()
Paging = Annotated[PageParams, Depends(page_params)]
RUN = tenant_with(Permission.RESEARCH_RUN)
REVIEW = tenant_with(Permission.RESEARCH_REVIEW)
READ = tenant_with(Permission.CRM_READ)


class ConfigIn(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}
    name: str = Field(min_length=1, max_length=120)
    country: str = Field(min_length=2, max_length=100)
    cities: list[str] = Field(min_length=1, max_length=50)
    categories: list[str] = Field(min_length=1, max_length=50)
    services: list[str] = Field(default_factory=list, max_length=50)
    ideal_customer: str | None = Field(default=None, max_length=2000)
    exclusions: dict[str, list[str]] = Field(default_factory=dict)
    depth: Literal["listing_only", "homepage", "site"] = "homepage"
    draft_language: str = Field(default="bg", pattern=r"^[a-z]{2}$")
    cost_cap: Decimal = Field(ge=0, max_digits=14, decimal_places=4)
    candidate_cap: int = Field(ge=1, le=2000)
    qualified_target: int = Field(ge=1, le=2000)
    cadence: Literal["manual", "daily", "weekly"] = "manual"
    run_at_local_time: str = Field(default="06:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    discovery_connection_id: UUID | None = None
    model_connection_id: UUID | None = None
    is_active: bool = True


class ConfigOut(ConfigIn):
    id: UUID
    next_run_at: datetime | None
    created_at: datetime


class RunStart(BaseModel):
    mode: Literal["discover", "refresh"] = "discover"


class RunOut(BaseModel):
    id: UUID
    config_id: UUID
    mode: str
    trigger: str
    status: Literal["queued", "running", "paused", "cancelling", "cancelled", "completed", "failed"]
    counters: dict[str, Any]
    summary: dict[str, Any]
    estimated_cost: Decimal
    error: str | None
    searches_total: int
    searches_done: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class CandidateOut(BaseModel):
    id: UUID
    run_id: UUID
    state: str
    state_reason: str | None
    listing_id_type: str
    listing_id: str | None
    name: str | None
    name_source: str | None
    city: str | None
    category: str | None
    website_url: str | None
    website_source: str | None
    website_match: str
    duplicate_lead_id: UUID | None
    inspection: dict[str, Any]
    proposal: dict[str, Any]
    validation_issues: list[dict[str, str]]
    dropped_fields: list[str] = Field(
        description="Provider fields that were not stored because the source policy does not allow it"
    )
    model_name: str | None
    prompt_version: str | None
    promoted_lead_id: UUID | None
    expires_at: datetime | None
    created_at: datetime


class Promote(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}
    name: str | None = Field(
        default=None, min_length=1, max_length=300, description="Required when the candidate has no stored name"
    )
    confirmed_by_hand: bool = Field(
        default=False, description="Set when a person confirmed a business that had no readable website"
    )
    manual_phone: str | None = Field(default=None, max_length=100)
    manual_source_note: str | None = Field(default=None, max_length=500)


class Reject(BaseModel):
    reason: str = Field(min_length=2, max_length=500)


class PolicyOut(BaseModel):
    id: UUID
    source_type: str
    field: str
    may_store: bool
    retention_days: int | None
    may_export: bool
    may_use_for_scoring: bool
    status: Literal["approved", "unverified", "quarantined"]
    note: str | None
    reviewed_at: datetime | None


class PolicyUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    status: Literal["approved", "unverified", "quarantined"]
    may_store: bool = False
    may_export: bool = False
    may_use_for_scoring: bool = False
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    note: str = Field(min_length=5, max_length=1000, description="What was verified, where, and when")


CONFIG_COLUMNS = (
    "name",
    "country",
    "cities",
    "categories",
    "services",
    "ideal_customer",
    "depth",
    "draft_language",
    "cost_cap",
    "candidate_cap",
    "qualified_target",
    "cadence",
    "run_at_local_time",
    "discovery_connection_id",
    "model_connection_id",
    "is_active",
)


def _config(row: Any) -> ConfigOut:
    data = dict(row)
    data["run_at_local_time"] = data["run_at_local_time"].strftime("%H:%M")
    return ConfigOut(**{k: data[k] for k in ConfigOut.model_fields})


def _check_connections(ctx: TenantContext, body: ConfigIn) -> None:
    for column, purpose in (("discovery_connection_id", "discovery"), ("model_connection_id", "model")):
        value = getattr(body, column)
        if value is None:
            continue
        found = scalar(
            ctx, "SELECT purpose FROM provider_connections WHERE tenant_id = :tenant_id AND id = :c", {"c": value}
        )
        if found != purpose:
            raise ValidationFailed(f"Choose a {purpose} connection from this workspace.")


def _save_config(ctx: TenantContext, body: ConfigIn, config_id: UUID | None) -> ConfigOut:
    _check_connections(ctx, body)
    timezone = scalar(ctx, "SELECT timezone FROM tenants WHERE id = :tenant_id")
    hour, minute = (int(x) for x in body.run_at_local_time.split(":"))
    from datetime import time as clock

    local = clock(hour, minute)
    upcoming = orchestrator.next_run_at(body.cadence, local, timezone) if body.is_active else None
    values = {
        **body.model_dump(exclude={"exclusions", "run_at_local_time"}),
        "run_at_local_time": local,
        "exclusions": json.dumps(body.exclusions),
        "next": upcoming,
        "u": ctx.user_id,
    }
    columns = ", ".join(CONFIG_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in CONFIG_COLUMNS)
    try:
        if config_id is None:
            row = one(
                ctx,
                f"INSERT INTO research_configs (tenant_id, {columns}, exclusions, next_run_at, created_by) "
                f"VALUES (:tenant_id, {placeholders}, CAST(:exclusions AS jsonb), :next, :u) RETURNING *",
                values,
                "Configuration not found.",
            )
        else:
            assignments = ", ".join(f"{c} = :{c}" for c in CONFIG_COLUMNS)
            row = one(
                ctx,
                f"UPDATE research_configs SET {assignments}, exclusions = CAST(:exclusions AS jsonb), next_run_at = :next "
                "WHERE tenant_id = :tenant_id AND id = :id RETURNING *",
                {**values, "id": config_id},
                "Configuration not found.",
            )
    except Exception as exc:
        if "uq_research_configs_name" in str(exc):
            raise ConflictError("A research configuration with that name already exists.") from exc
        raise
    # The schedule is a due row, replaced whenever the configuration changes.
    execute(
        ctx,
        "DELETE FROM due_jobs WHERE tenant_id = :tenant_id AND kind = 'research.schedule' AND ref_id = :c AND status = 'pending'",
        {"c": row["id"]},
    )
    if upcoming is not None:
        schedule(
            ctx.db,
            ctx.tenant_id,
            kind="research.schedule",
            unique_key=f"{row['id']}:{upcoming.isoformat()}",
            due_at=upcoming,
            ref_id=row["id"],
        )
    return _config(row)


# --- configurations --------------------------------------------------------------------------


@router.get("/research-configs", response_model=list[ConfigOut], operation_id="listResearchConfigs", tags=["research"])
def list_configs(ctx: TenantContext = READ) -> list[ConfigOut]:
    return [
        _config(r)
        for r in many(ctx, "SELECT * FROM research_configs WHERE tenant_id = :tenant_id ORDER BY lower(name)")
    ]


@router.post(
    "/research-configs",
    response_model=ConfigOut,
    status_code=201,
    operation_id="createResearchConfig",
    tags=["research"],
)
def create_config(body: ConfigIn, ctx: TenantContext = RUN) -> ConfigOut:
    ctx.require_person("Setting what a research run may search and spend")
    return _save_config(ctx, body, None)


@router.put(
    "/research-configs/{config_id}", response_model=ConfigOut, operation_id="updateResearchConfig", tags=["research"]
)
def update_config(config_id: UUID, body: ConfigIn, ctx: TenantContext = RUN) -> ConfigOut:
    ctx.require_person("Changing what a research run may search and spend")
    exists(ctx, "research_configs", config_id, "The configuration")
    return _save_config(ctx, body, config_id)


# --- runs ------------------------------------------------------------------------------------

RUN_SQL = """
    SELECT r.*, (SELECT count(*) FROM research_queries q WHERE q.tenant_id = r.tenant_id AND q.run_id = r.id) AS searches_total,
           (SELECT count(*) FROM research_queries q WHERE q.tenant_id = r.tenant_id AND q.run_id = r.id AND q.status <> 'pending') AS searches_done
    FROM research_runs r WHERE r.tenant_id = :tenant_id
"""


def _run(ctx: TenantContext, run_id: UUID) -> RunOut:
    return RunOut(**one(ctx, f"{RUN_SQL} AND r.id = :id", {"id": run_id}, "Run not found."))


@router.post(
    "/research-configs/{config_id}/runs",
    response_model=RunOut,
    status_code=201,
    operation_id="startResearchRun",
    tags=["research"],
)
def start_run(config_id: UUID, body: RunStart, ctx: TenantContext = RUN) -> RunOut:
    """Start a run. It needs a budget and two connected providers; it never spends beyond its cost cap."""
    if body.mode == "refresh":
        raise ValidationFailed("Refreshing existing leads is not available yet. Start a discovery run instead.")
    try:
        run_id = orchestrator.create_run(
            ctx.db, ctx.tenant_id, config_id, mode=body.mode, trigger="manual", requested_by=ctx.user_id
        )
    except orchestrator.RunBlocked as exc:
        raise ConflictError(str(exc)) from exc
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="research.run_started",
        target_type="research_run",
        target_id=str(run_id),
        data={"config_id": str(config_id), "mode": body.mode},
    )
    return _run(ctx, run_id)


@router.get("/research-runs", response_model=Page[RunOut], operation_id="listResearchRuns", tags=["research"])
def list_runs(paging: Paging, ctx: TenantContext = READ) -> Page[RunOut]:
    total = scalar(ctx, "SELECT count(*) FROM research_runs WHERE tenant_id = :tenant_id")
    rows = many(
        ctx,
        f"{RUN_SQL} ORDER BY r.created_at DESC, r.id LIMIT :limit OFFSET :offset",
        {"limit": paging.limit, "offset": paging.offset},
    )
    return Page(items=[RunOut(**r) for r in rows], total=total, limit=paging.limit, offset=paging.offset)


@router.get("/research-runs/{run_id}", response_model=RunOut, operation_id="getResearchRun", tags=["research"])
def get_run(run_id: UUID, ctx: TenantContext = READ) -> RunOut:
    return _run(ctx, run_id)


@router.post(
    "/research-runs/{run_id}/{action}", response_model=RunOut, operation_id="controlResearchRun", tags=["research"]
)
def control_run(run_id: UUID, action: Literal["pause", "resume", "cancel"], ctx: TenantContext = RUN) -> RunOut:
    current = one(
        ctx,
        "SELECT status FROM research_runs WHERE tenant_id = :tenant_id AND id = :id FOR UPDATE",
        {"id": run_id},
        "Run not found.",
    )["status"]
    transitions = {
        "pause": ({"queued", "running"}, "paused"),
        "resume": ({"paused"}, "running"),
        # Cancelling lets the step in flight finish and be accounted for; no new work starts.
        "cancel": ({"queued", "running", "paused"}, "cancelling"),
    }
    allowed, target = transitions[action]
    if current not in allowed:
        raise ConflictError(
            f"A run that is {current} cannot be {action}d."
            if action != "resume"
            else f"A run that is {current} cannot be resumed."
        )
    execute(
        ctx,
        "UPDATE research_runs SET status = :s, error = NULL WHERE tenant_id = :tenant_id AND id = :id",
        {"s": target, "id": run_id},
    )
    if action in ("resume", "cancel"):
        execute(
            ctx,
            "DELETE FROM due_jobs WHERE tenant_id = :tenant_id AND kind = 'research.run' AND ref_id = :id AND status IN ('done', 'failed')",
            {"id": run_id},
        )
        schedule(ctx.db, ctx.tenant_id, kind="research.run", unique_key=str(run_id), ref_id=run_id)
        execute(
            ctx,
            "UPDATE due_jobs SET status = 'pending', due_at = now(), lease_token = NULL WHERE tenant_id = :tenant_id "
            "AND kind = 'research.run' AND ref_id = :id AND status <> 'claimed'",
            {"id": run_id},
        )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action=f"research.run_{action}",
        target_type="research_run",
        target_id=str(run_id),
    )
    return _run(ctx, run_id)


@router.get("/research-runs/{run_id}/queries", operation_id="listResearchQueries", tags=["research"])
def list_queries(run_id: UUID, ctx: TenantContext = READ) -> list[dict[str, Any]]:
    """Search provenance: what was asked, of whom, when, and what came back."""
    _run(ctx, run_id)
    rows = many(
        ctx,
        "SELECT position, city, category, provider, query_text, status, result_count, coverage_gap, executed_at "
        "FROM research_queries WHERE tenant_id = :tenant_id AND run_id = :r ORDER BY position",
        {"r": run_id},
    )
    return [dict(r) for r in rows]


# --- review queue ----------------------------------------------------------------------------


@router.get(
    "/research-candidates", response_model=Page[CandidateOut], operation_id="listResearchCandidates", tags=["research"]
)
def list_candidates(
    paging: Paging,
    ctx: TenantContext = READ,
    run_id: UUID | None = None,
    state: Literal["new", "duplicate", "excluded", "needs_review", "qualified", "rejected", "promoted", "error"]
    | None = None,
) -> Page[CandidateOut]:
    where = ["tenant_id = :tenant_id"]
    typed: dict[str, Any] = {}
    if run_id:
        where.append("run_id = :run_id")
        typed["run_id"] = run_id
    if state:
        where.append("state = :state")
        typed["state"] = state
    condition = " AND ".join(where)
    total = scalar(ctx, f"SELECT count(*) FROM research_candidates WHERE {condition}", typed)
    rows = many(
        ctx,
        f"SELECT * FROM research_candidates WHERE {condition} ORDER BY (proposal #>> '{{score,total}}')::int DESC NULLS LAST, "
        "created_at, id LIMIT :limit OFFSET :offset",
        {**typed, "limit": paging.limit, "offset": paging.offset},
    )
    return Page(items=[CandidateOut(**r) for r in rows], total=total, limit=paging.limit, offset=paging.offset)


@router.post(
    "/research-candidates/{candidate_id}/reject",
    response_model=CandidateOut,
    operation_id="rejectResearchCandidate",
    tags=["research"],
)
def reject_candidate(candidate_id: UUID, body: Reject, ctx: TenantContext = REVIEW) -> CandidateOut:
    row = one(
        ctx,
        "UPDATE research_candidates SET state = 'rejected', state_reason = :r, reviewed_by = :u, reviewed_at = now() "
        "WHERE tenant_id = :tenant_id AND id = :id AND state IN ('needs_review', 'qualified') RETURNING *",
        {"r": body.reason, "u": ctx.user_id, "id": candidate_id},
        "Candidate not found or already decided.",
    )
    return CandidateOut(**row)


@router.post(
    "/research-candidates/{candidate_id}/promote",
    response_model=CandidateOut,
    operation_id="promoteResearchCandidate",
    tags=["research"],
)
def promote_candidate(candidate_id: UUID, body: Promote, ctx: TenantContext = REVIEW) -> CandidateOut:
    """A reviewer accepts a candidate. Only then does it become a lead, with its evidence and a proposed score."""
    candidate = one(
        ctx,
        "SELECT * FROM research_candidates WHERE tenant_id = :tenant_id AND id = :id FOR UPDATE",
        {"id": candidate_id},
        "Candidate not found.",
    )
    if candidate["state"] not in ("needs_review", "qualified"):
        raise ConflictError("This candidate was already decided.")
    from app.modules.dataops import service as dataops

    if dataops.is_erased(
        ctx.db, ctx.tenant_id, [("domain", candidate["domain"]), ("listing_id", candidate["listing_id"])]
    ):
        raise ConflictError("This business was erased on request and cannot be added again from research.")
    proposal = candidate["proposal"] or {}
    name = body.name or candidate["name"]
    if not name:
        raise ValidationFailed("Give the business name. Nothing reliable enough to store was found automatically.")
    has_evidence = bool(proposal.get("observations"))
    if not has_evidence and not body.confirmed_by_hand:
        raise ValidationFailed("This candidate has no evidence from its own website. Confirm it by hand to add it.")
    tenant = one(ctx, "SELECT timezone FROM tenants WHERE id = :tenant_id", {}, "Workspace not found.")
    today = today_in(tenant["timezone"])
    country = scalar(
        ctx,
        "SELECT settings->>'country' FROM research_runs WHERE tenant_id = :tenant_id AND id = :r",
        {"r": candidate["run_id"]},
    )
    name_source = "manual_entry" if body.name else (candidate["name_source"] or "manual_entry")

    company_id = scalar(
        ctx,
        "INSERT INTO companies (tenant_id, name, business_type, city, country, website_url, domain, source) "
        "VALUES (:tenant_id, :n, :cat, :city, :country, :web, :domain, 'research') RETURNING id",
        {
            "n": name,
            "cat": candidate["category"],
            "city": candidate["city"],
            "country": country,
            "web": candidate["website_url"],
            "domain": domain_of(candidate["website_url"]),
        },
    )
    lead_id = scalar(
        ctx,
        "INSERT INTO leads (tenant_id, company_id, status, source) VALUES (:tenant_id, :c, 'needs_review', 'research') RETURNING id",
        {"c": company_id},
    )
    for contact in proposal.get("contacts", []):
        if contact["kind"] == "phone":
            normalized, e164 = normalize_phone(contact["value"], country)
        else:
            normalized, e164 = normalize_email(contact["value"]), False
        if normalized is None:
            continue
        execute(
            ctx,
            "INSERT INTO contact_channels (tenant_id, company_id, kind, raw_value, normalized_value, normalized_is_e164, source_type, "
            "source_url, source_date, position) VALUES (:tenant_id, :c, :k, :raw, :n, :e, 'official_website', :url, :d, "
            "(SELECT COALESCE(max(position), 0) + 1 FROM contact_channels WHERE tenant_id = :tenant_id AND company_id = :c))",
            {
                "c": company_id,
                "k": contact["kind"],
                "raw": contact["value"],
                "n": normalized,
                "e": e164,
                "url": contact.get("source_url"),
                "d": today,
            },
        )
    if body.manual_phone:
        normalized, e164 = normalize_phone(body.manual_phone, country)
        if normalized is None:
            raise ValidationFailed("Enter a valid phone number.")
        execute(
            ctx,
            "INSERT INTO contact_channels (tenant_id, company_id, kind, raw_value, normalized_value, normalized_is_e164, source_type, "
            "label, source_date) VALUES (:tenant_id, :c, 'phone', :raw, :n, :e, 'manual_entry', :label, :d)",
            {
                "c": company_id,
                "raw": body.manual_phone,
                "n": normalized,
                "e": e164,
                "label": body.manual_source_note,
                "d": today,
            },
        )

    inspection = candidate["inspection"] or {}
    status_raw = (
        ("Loads (HTTPS)" if inspection.get("https") else "Loads (HTTP only)")
        if inspection.get("status") == 200
        else None
    )
    execute(
        ctx,
        "INSERT INTO lead_assessments (tenant_id, lead_id, checked_on, confidence, source_type, verification_state, website_status_raw, "
        "website_base, website_availability, website_transport, website_match, listing_id_type, listing_id, recommended_service_raw, "
        "outreach_opening, discovery_question, notes, raw_row) VALUES (:tenant_id, :l, :d, :conf, :src, 'unverified', :raw, :base, :avail, "
        ":transport, :match, :lt, :li, :service, :opening, :question, :notes, CAST(:meta AS jsonb))",
        {
            "l": lead_id,
            "d": today,
            "conf": proposal.get("confidence") or "unknown",
            "src": "ai_derived" if has_evidence else "manual_entry",
            "raw": status_raw,
            "base": "loads" if status_raw else "unknown",
            "avail": "available" if status_raw else "unknown",
            "transport": ("https" if inspection.get("https") else "http_only") if status_raw else "unknown",
            "match": "assumed" if candidate["website_match"] == "confirmed" else "unknown",
            "lt": candidate["listing_id_type"],
            "li": candidate["listing_id"],
            "service": proposal.get("recommended_service"),
            "opening": proposal.get("opening"),
            "question": proposal.get("discovery_question"),
            "notes": body.manual_source_note or inspection.get("limitation"),
            "meta": json.dumps(
                {
                    "research_candidate_id": str(candidate_id),
                    "model": candidate["model_name"],
                    "prompt_version": candidate["prompt_version"],
                    "name_source": name_source,
                    "unsupported_checks": inspection.get("unsupported_checks", []),
                }
            ),
        },
    )
    for observation in proposal.get("observations", []):
        execute(
            ctx,
            "INSERT INTO observations (tenant_id, lead_id, text, source_type, evidence_url, observed_on, created_by) "
            "VALUES (:tenant_id, :l, :x, 'ai_derived', :url, :d, :u)",
            {"l": lead_id, "x": observation["text"], "url": observation["evidence_url"], "d": today, "u": ctx.user_id},
        )
    for hypothesis in proposal.get("hypotheses", []):
        execute(
            ctx,
            "INSERT INTO hypotheses (tenant_id, lead_id, text, source_type, created_by) VALUES (:tenant_id, :l, :x, 'ai_derived', :u)",
            {"l": lead_id, "x": hypothesis, "u": ctx.user_id},
        )
    score = proposal.get("score")
    if score:
        rubric_id, rubric = active_rubric(ctx.db, ctx.tenant_id)
        try:  # recomputed here: a stored proposal is never trusted for its total or tier
            computed = compute_score(dict(score["components"]), rubric)
        except ScoreError:
            computed = None
        if computed is not None:
            execute(
                ctx,
                "INSERT INTO lead_scores (tenant_id, lead_id, rubric_version_id, components, total, tier, origin, reasons, created_by) "
                "VALUES (:tenant_id, :l, :r, CAST(:c AS jsonb), :total, :tier, 'ai_proposal', CAST(:why AS jsonb), :u)",
                {
                    "l": lead_id,
                    "r": rubric_id,
                    "c": json.dumps(computed.components),
                    "total": computed.total,
                    "tier": computed.tier,
                    "why": json.dumps(proposal.get("component_reasons") or {}),
                    "u": ctx.user_id,
                },
            )
    row = one(
        ctx,
        "UPDATE research_candidates SET state = 'promoted', promoted_lead_id = :l, reviewed_by = :u, reviewed_at = now(), expires_at = NULL "
        "WHERE tenant_id = :tenant_id AND id = :id RETURNING *",
        {"l": lead_id, "u": ctx.user_id, "id": candidate_id},
        "Candidate not found.",
    )
    log_activity(
        ctx,
        "lead.researched",
        "Added from a research run after review",
        company_id=company_id,
        lead_id=lead_id,
        data={"research_candidate_id": str(candidate_id), "confirmed_by_hand": body.confirmed_by_hand},
        origin="research",
    )
    return CandidateOut(**row)


# --- source policies -------------------------------------------------------------------------


@router.get("/source-policies", response_model=list[PolicyOut], operation_id="listSourcePolicies", tags=["research"])
def list_policies(ctx: TenantContext = READ) -> list[PolicyOut]:
    return [
        PolicyOut(**r)
        for r in many(ctx, "SELECT * FROM source_policies WHERE tenant_id = :tenant_id ORDER BY source_type, field")
    ]


@router.put(
    "/source-policies/{policy_id}", response_model=PolicyOut, operation_id="updateSourcePolicy", tags=["research"]
)
def update_policy(
    policy_id: UUID, body: PolicyUpdate, ctx: TenantContext = tenant_with(Permission.TENANT_SETTINGS)
) -> PolicyOut:
    """Record the outcome of reviewing a source's terms. Storage is only possible once a source is approved."""
    if body.status != "approved" and (body.may_store or body.may_export or body.may_use_for_scoring):
        raise ValidationFailed("A source that is not approved cannot be stored, exported or used for scoring.")
    row = one(
        ctx,
        "UPDATE source_policies SET status = :status, may_store = :may_store, may_export = :may_export, "
        "may_use_for_scoring = :may_use_for_scoring, retention_days = :retention_days, note = :note, reviewed_by = :u, reviewed_at = now() "
        "WHERE tenant_id = :tenant_id AND id = :id RETURNING *",
        {**body.model_dump(), "u": ctx.user_id, "id": policy_id},
        "Policy not found.",
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="source_policy.updated",
        target_type="source_policy",
        target_id=str(policy_id),
        data={"source_type": row["source_type"], "field": row["field"], "status": body.status, "note": body.note},
    )
    return PolicyOut(**row)
