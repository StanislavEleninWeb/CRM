"""Retention settings, erasure on request, workspace export and deletion, and support access."""

import io
import json
import zipfile
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.deps import TenantContext, tenant_with
from app.core.errors import ConflictError
from app.core.normalize import normalize_email
from app.core.time import utcnow
from app.modules.crm.common import ValidationFailed, execute, many, one, scalar
from app.modules.dataops import service
from app.modules.identity.audit import record_audit
from app.modules.identity.permissions import Permission
from app.worker.due import schedule

router = APIRouter()
SETTINGS = tenant_with(Permission.TENANT_SETTINGS)
OWNER = tenant_with(Permission.OWNERS_MANAGE)
DELETE = tenant_with(Permission.CRM_DELETE)

# What a workspace export contains. Stored credentials, token hashes and keys are never exported.
EXPORT_TABLES = (
    "companies", "contacts", "contact_channels", "contact_restrictions", "leads", "lead_assessments", "observations", "hypotheses",
    "lead_scores", "deals", "deal_stage_changes", "tasks", "notes", "activities", "call_attempts", "email_threads", "email_messages",
    "email_drafts", "send_intents", "email_suppressions", "email_consents", "recipient_profiles", "research_configs", "research_runs",
    "research_candidates", "usage_ledger", "budgets",
)  # fmt: skip


class RetentionOut(BaseModel):
    email_content_days: int
    webhook_delivery_days: int
    outbox_event_days: int
    finished_job_days: int
    idempotency_hours: int
    audit_days: int
    bounds: dict[str, tuple[int, int]]
    not_covered: list[str]


class RetentionIn(BaseModel):
    model_config = {"extra": "forbid"}
    email_content_days: int | None = None
    webhook_delivery_days: int | None = None
    outbox_event_days: int | None = None
    finished_job_days: int | None = None
    idempotency_hours: int | None = None
    audit_days: int | None = None


NOT_COVERED = [
    "Backups: a deleted record remains in encrypted backups until they expire (see the retention document for the window).",
    "The security log: who did what is kept for the audit period even when the record it refers to is gone.",
    "Copies already exported or sent by webhook to your own systems.",
    "Mail in the mailbox itself: this application deletes its copy, never the mailbox's.",
]


def _retention(ctx: TenantContext) -> RetentionOut:
    return RetentionOut(**service.settings_for(ctx.db, ctx.tenant_id), bounds=service.BOUNDS, not_covered=NOT_COVERED)


@router.get("/retention", response_model=RetentionOut, operation_id="getRetention", tags=["data"])
def get_retention(ctx: TenantContext = SETTINGS) -> RetentionOut:
    return _retention(ctx)


@router.put("/retention", response_model=RetentionOut, operation_id="setRetention", tags=["data"])
def set_retention(body: RetentionIn, ctx: TenantContext = SETTINGS) -> RetentionOut:
    changes = body.model_dump(exclude_none=True)
    for key, value in changes.items():
        low, high = service.BOUNDS[key]
        if not low <= value <= high:
            raise ValidationFailed(f"{key.replace('_', ' ')} must be between {low} and {high}.")
    current = service.settings_for(ctx.db, ctx.tenant_id)
    execute(
        ctx,
        "UPDATE tenants SET retention = CAST(:r AS jsonb) WHERE id = :tenant_id",
        {"r": json.dumps({**current, **changes})},
    )
    record_audit(
        ctx.db, tenant_id=ctx.tenant_id, actor_id=ctx.user_id, action="retention.changed", target_type="tenant",
        target_id=str(ctx.tenant_id), data=changes,
    )  # fmt: skip
    return _retention(ctx)


class EraseIn(BaseModel):
    reason: str = Field(
        min_length=10, max_length=500, description="For example: erasure requested by the business on a given date"
    )
    confirm_name: str = Field(description="The company's name, typed again")


@router.post("/companies/{company_id}/erase", operation_id="eraseCompany", tags=["data"])
def erase_company(company_id: UUID, body: EraseIn, ctx: TenantContext = DELETE) -> dict[str, Any]:
    """Remove a business and everything recorded about it, and keep it from coming back through an import, research or the mailbox."""
    ctx.require_person("Erasing a business on request")
    name = scalar(ctx, "SELECT name FROM companies WHERE tenant_id = :tenant_id AND id = :id", {"id": company_id})
    if name is None:
        one(ctx, "SELECT 1 WHERE false", {}, "Company not found.")
    if body.confirm_name.strip().casefold() != str(name).strip().casefold():
        raise ValidationFailed("The name does not match. Nothing was erased.")
    try:
        counts = service.erase_company(ctx.db, ctx.tenant_id, company_id, reason=body.reason, erased_by=ctx.user_id)
    except service.ErasureBlocked as exc:
        raise ConflictError(str(exc)) from exc
    # The audit entry records that it happened and why, without the name or any contact detail.
    record_audit(
        ctx.db, tenant_id=ctx.tenant_id, actor_id=ctx.user_id, action="company.erased", target_type="company",
        target_id=str(company_id), data={"reason": body.reason, "removed": counts},
    )  # fmt: skip
    return {"erased": True, "removed": counts}


@router.get("/exports/workspace.zip", operation_id="exportWorkspace", tags=["data"])
def export_workspace(ctx: TenantContext = OWNER) -> Response:
    """Everything the workspace holds, as one JSON file per kind of record. Credentials and keys are not included."""
    buffer = io.BytesIO()
    manifest: dict[str, Any] = {"exported_at": utcnow().isoformat(), "tenant_id": str(ctx.tenant_id), "files": {}}
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for table in EXPORT_TABLES:
            rows = many(ctx, f"SELECT * FROM {table} WHERE tenant_id = :tenant_id ORDER BY created_at")
            archive.writestr(
                f"{table}.json", json.dumps([dict(r) for r in rows], default=_plain, ensure_ascii=False, indent=1)
            )
            manifest["files"][f"{table}.json"] = len(rows)
        manifest["not_included"] = [
            "stored credentials and tokens",
            "API key and session hashes",
            "attachment files (listed in activities)",
        ]
        archive.writestr("manifest.json", json.dumps(manifest, indent=1))
    record_audit(
        ctx.db, tenant_id=ctx.tenant_id, actor_id=ctx.user_id, action="workspace.exported", target_type="tenant",
        target_id=str(ctx.tenant_id), data={"records": sum(manifest["files"].values())},
    )  # fmt: skip
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="workspace-export.zip"', "Cache-Control": "no-store"},
    )


def _plain(value: Any) -> Any:
    if isinstance(value, (bytes, memoryview)):
        return None  # binary columns are never exported
    return str(value)


class DeletionOut(BaseModel):
    requested_at: datetime | None
    due_at: datetime | None
    grace_days: int
    what_happens: list[str]


class DeletionIn(BaseModel):
    confirm_name: str


WHAT_HAPPENS = [
    "For seven days nothing changes and the request can be cancelled.",
    "Then every record, file, stored credential and mailbox authorisation of this workspace is deleted.",
    "Encrypted backups made before then still contain the data until they expire; it is not restored from them except to recover from a failure.",
    "A subscription is not cancelled by this. Cancel it in billing first.",
]


def _deletion(ctx: TenantContext) -> DeletionOut:
    row = one(ctx, "SELECT deletion_requested_at, deletion_due_at FROM tenants WHERE id = :tenant_id", {}, "Not found.")
    return DeletionOut(
        requested_at=row["deletion_requested_at"],
        due_at=row["deletion_due_at"],
        grace_days=service.DELETION_GRACE.days,
        what_happens=WHAT_HAPPENS,
    )


@router.get("/tenant/deletion", response_model=DeletionOut, operation_id="getWorkspaceDeletion", tags=["data"])
def get_deletion(ctx: TenantContext = OWNER) -> DeletionOut:
    return _deletion(ctx)


@router.post("/tenant/deletion", response_model=DeletionOut, operation_id="requestWorkspaceDeletion", tags=["data"])
def request_deletion(body: DeletionIn, ctx: TenantContext = OWNER) -> DeletionOut:
    ctx.require_person("Deleting a workspace")
    name = scalar(ctx, "SELECT name FROM tenants WHERE id = :tenant_id")
    if body.confirm_name.strip() != name:
        raise ValidationFailed("The workspace name does not match. Nothing was scheduled.")
    live = scalar(
        ctx,
        "SELECT status FROM tenant_billing WHERE tenant_id = :tenant_id AND provider_subscription_id IS NOT NULL "
        "AND status IN ('active', 'trialing', 'past_due', 'unpaid', 'incomplete')",
    )
    if live:
        raise ConflictError("A subscription is still running and would keep being charged. Cancel it in billing first.")
    due = utcnow() + service.DELETION_GRACE
    execute(
        ctx,
        "UPDATE tenants SET deletion_requested_at = now(), deletion_due_at = :d, deletion_requested_by = :u WHERE id = :tenant_id "
        "AND deletion_due_at IS NULL",
        {"d": due, "u": ctx.user_id},
    )
    schedule(ctx.db, ctx.tenant_id, kind=service.DELETE_KIND, unique_key="deletion", due_at=due)
    execute(
        ctx,
        "UPDATE due_jobs SET status = 'pending', due_at = (SELECT deletion_due_at FROM tenants WHERE id = :tenant_id), finished_at = NULL, "
        "attempts = 0 WHERE tenant_id = :tenant_id AND kind = :k",
        {"k": service.DELETE_KIND},
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="workspace.deletion_requested",
        target_type="tenant",
        target_id=str(ctx.tenant_id),
    )
    return _deletion(ctx)


@router.delete("/tenant/deletion", response_model=DeletionOut, operation_id="cancelWorkspaceDeletion", tags=["data"])
def cancel_deletion(ctx: TenantContext = OWNER) -> DeletionOut:
    execute(
        ctx,
        "UPDATE tenants SET deletion_requested_at = NULL, deletion_due_at = NULL, deletion_requested_by = NULL WHERE id = :tenant_id",
    )
    execute(
        ctx,
        "UPDATE due_jobs SET status = 'cancelled', finished_at = now() WHERE tenant_id = :tenant_id AND kind = :k AND status = 'pending'",
        {"k": service.DELETE_KIND},
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="workspace.deletion_cancelled",
        target_type="tenant",
        target_id=str(ctx.tenant_id),
    )
    return _deletion(ctx)


# --- support access --------------------------------------------------------------------------


class GrantOut(BaseModel):
    id: UUID
    grantee_email: str
    include_communications: bool
    reason: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    first_used_at: datetime | None
    active: bool


class GrantIn(BaseModel):
    model_config = {"extra": "forbid"}
    grantee_email: str = Field(max_length=254)
    hours: int = Field(default=24, ge=1, le=72)
    reason: str = Field(min_length=10, max_length=500)
    include_communications: bool = Field(
        default=False, description="Whether the person may read email conversations and drafts"
    )


GRANT_SQL = (
    "SELECT g.id, g.grantee_email::text AS grantee_email, g.include_communications, g.reason, g.created_at, g.expires_at, g.revoked_at, "
    "g.first_used_at, (g.revoked_at IS NULL AND g.expires_at > now()) AS active FROM support_grants g WHERE g.tenant_id = :tenant_id"
)


@router.get("/support-grants", response_model=list[GrantOut], operation_id="listSupportGrants", tags=["support access"])
def list_grants(ctx: TenantContext = OWNER) -> list[GrantOut]:
    return [GrantOut(**r) for r in many(ctx, f"{GRANT_SQL} ORDER BY g.created_at DESC LIMIT 100")]


@router.post(
    "/support-grants",
    response_model=GrantOut,
    status_code=201,
    operation_id="createSupportGrant",
    tags=["support access"],
)
def create_grant(body: GrantIn, ctx: TenantContext = OWNER) -> GrantOut:
    """Let one named person look at this workspace, read-only, for a limited time. Nobody has this access otherwise."""
    ctx.require_person("Granting support access")
    email = normalize_email(body.grantee_email)
    if email is None:
        raise ValidationFailed("Enter a valid email address.")
    # Whether that address has an account here is not revealed: the grant simply takes effect when its owner signs in.
    if scalar(
        ctx,
        "SELECT 1 FROM memberships m JOIN users u ON u.id = m.user_id WHERE m.tenant_id = :tenant_id AND u.email = :e",
        {"e": email},
    ):
        raise ConflictError("That person is already a member of this workspace.")
    grant_id = scalar(
        ctx,
        "INSERT INTO support_grants (tenant_id, grantee_email, include_communications, reason, granted_by, expires_at) "
        "VALUES (:tenant_id, :mail, :c, :r, :u, :e) RETURNING id",
        {
            "mail": email,
            "c": body.include_communications,
            "r": body.reason.strip(),
            "u": ctx.user_id,
            "e": utcnow() + timedelta(hours=body.hours),
        },
    )
    record_audit(
        ctx.db, tenant_id=ctx.tenant_id, actor_id=ctx.user_id, action="support.access_granted", target_type="support_grant",
        target_id=str(grant_id), data={"hours": body.hours, "include_communications": body.include_communications, "reason": body.reason.strip()},
    )  # fmt: skip
    return GrantOut(**one(ctx, f"{GRANT_SQL} AND g.id = :id", {"id": grant_id}, "Not found."))


@router.delete(
    "/support-grants/{grant_id}", response_model=GrantOut, operation_id="revokeSupportGrant", tags=["support access"]
)
def revoke_grant(grant_id: UUID, ctx: TenantContext = OWNER) -> GrantOut:
    one(
        ctx,
        "UPDATE support_grants SET revoked_at = COALESCE(revoked_at, now()) WHERE tenant_id = :tenant_id AND id = :id RETURNING id",
        {"id": grant_id},
        "Support access not found.",
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="support.access_revoked",
        target_type="support_grant",
        target_id=str(grant_id),
    )
    return GrantOut(**one(ctx, f"{GRANT_SQL} AND g.id = :id", {"id": grant_id}, "Not found."))
