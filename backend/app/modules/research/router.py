"""Imports, exports, rubric and scores."""

import json
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.deps import TenantContext, tenant_with
from app.core.errors import ConflictError
from app.core.pagination import Page, PageParams, page_params
from app.core.storage import Storage, get_storage, new_storage_key, safe_filename, sha256_of
from app.modules.crm.common import ValidationFailed, exists, log_activity, many, one, scalar
from app.modules.identity.audit import record_audit
from app.modules.identity.permissions import Permission
from app.modules.research.exporter import build_export
from app.modules.research.importer import FIELDS, active_rubric
from app.modules.research.scoring import ScoreError, compute_score
from app.modules.research.tasks import commit_import_task, parse_import
from app.modules.research.workbook import MAX_FILE_BYTES, XLSX_TYPE

router = APIRouter()
Paging = Annotated[PageParams, Depends(page_params)]
IMPORT = tenant_with(Permission.IMPORT_RUN)
READ = tenant_with(Permission.CRM_READ)


class ImportOut(BaseModel):
    id: UUID
    kind: str
    filename: str
    size_bytes: int
    status: Literal["uploaded", "parsing", "ready", "committing", "committed", "failed"]
    mapping: dict[str, Any]
    report: dict[str, Any]
    result: dict[str, Any]
    error: str | None
    created_at: Any
    committed_at: Any | None


class ImportRowOut(BaseModel):
    id: UUID
    row_number: int
    external_id: str | None
    business_name: str | None
    action: Literal["create", "update", "skip"]
    issues: list[dict[str, str]]
    duplicate_lead_id: UUID | None
    duplicate_reason: str | None
    score_total: int | None
    tier: str | None
    lead_id: UUID | None


class ImportRowUpdate(BaseModel):
    action: Literal["create", "update", "skip"]


class MappingUpdate(BaseModel):
    mapping: dict[str, str] = Field(description="Field name to column header")


class RubricOut(BaseModel):
    id: UUID
    version: int
    name: str
    components: list[dict[str, Any]]
    tier_a_min: int
    tier_b_min: int
    is_active: bool


class ScoreIn(BaseModel):
    components: dict[str, int]
    reason: str = Field(min_length=3, max_length=500)
    component_reasons: dict[str, str] = Field(default_factory=dict)


class ScoreOut(BaseModel):
    id: UUID
    lead_id: UUID
    rubric_version: int
    components: dict[str, int]
    total: int
    tier: str
    origin: str
    reasons: dict[str, Any]
    override_reason: str | None
    source_total: int | None
    source_tier: str | None
    is_current: bool
    created_by: UUID | None
    created_at: Any


def _import(ctx: TenantContext, import_id: UUID) -> ImportOut:
    return ImportOut(
        **one(
            ctx,
            "SELECT * FROM imports WHERE tenant_id = :tenant_id AND id = :id",
            {"id": import_id},
            "Import not found.",
        )
    )


@router.post("/imports", response_model=ImportOut, status_code=202, operation_id="createImport", tags=["imports"])
def create_import(
    file: Annotated[UploadFile, File()], storage: Annotated[Storage, Depends(get_storage)], ctx: TenantContext = IMPORT
) -> ImportOut:
    """Upload a prospect list. It is parsed in the background into a preview; nothing is imported yet."""
    filename = safe_filename(file.filename or "upload")
    lower = filename.lower()
    if not lower.endswith((".xlsx", ".csv")):
        raise ValidationFailed("Only .xlsx and .csv files are accepted.")
    data = file.file.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValidationFailed(f"Files are limited to {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    if not data:
        raise ValidationFailed("The file is empty.")
    kind = "xlsx" if lower.endswith(".xlsx") else "csv"
    key = new_storage_key(ctx.tenant_id, "imports")
    storage.put(key, data, XLSX_TYPE if kind == "xlsx" else "text/csv")
    import_id = scalar(
        ctx,
        "INSERT INTO imports (tenant_id, kind, filename, size_bytes, sha256, storage_key, created_by) "
        "VALUES (:tenant_id, :k, :f, :s, :h, :key, :u) RETURNING id",
        {"k": kind, "f": filename, "s": len(data), "h": sha256_of(data), "key": key, "u": ctx.user_id},
    )
    ctx.db.commit()  # the worker must be able to see the row
    parse_import.delay(str(ctx.tenant_id), str(import_id), str(ctx.user_id))
    return _import(ctx, import_id)


@router.get("/imports", response_model=Page[ImportOut], operation_id="listImports", tags=["imports"])
def list_imports(paging: Paging, ctx: TenantContext = IMPORT) -> Page[ImportOut]:
    total = scalar(ctx, "SELECT count(*) FROM imports WHERE tenant_id = :tenant_id")
    rows = many(
        ctx,
        "SELECT * FROM imports WHERE tenant_id = :tenant_id ORDER BY created_at DESC, id LIMIT :limit OFFSET :offset",
        {"limit": paging.limit, "offset": paging.offset},
    )
    return Page(items=[ImportOut(**r) for r in rows], total=total, limit=paging.limit, offset=paging.offset)


@router.get("/imports/{import_id}", response_model=ImportOut, operation_id="getImport", tags=["imports"])
def get_import(import_id: UUID, ctx: TenantContext = IMPORT) -> ImportOut:
    return _import(ctx, import_id)


@router.get(
    "/imports/{import_id}/rows", response_model=Page[ImportRowOut], operation_id="listImportRows", tags=["imports"]
)
def list_import_rows(
    import_id: UUID,
    paging: Paging,
    ctx: TenantContext = IMPORT,
    only: Annotated[Literal["issues", "duplicates", "all"], Query()] = "all",
) -> Page[ImportRowOut]:
    _import(ctx, import_id)
    condition = "tenant_id = :tenant_id AND import_id = :i"
    if only == "issues":
        condition += " AND jsonb_path_exists(issues, '$[*] ? (@.severity != \"info\")')"
    elif only == "duplicates":
        condition += " AND duplicate_lead_id IS NOT NULL"
    total = scalar(ctx, f"SELECT count(*) FROM import_rows WHERE {condition}", {"i": import_id})
    rows = many(
        ctx,
        f"""
        SELECT id, row_number, external_id, data #>> '{{company,name}}' AS business_name, action, issues,
               duplicate_lead_id, duplicate_reason, (data #>> '{{score,total}}')::int AS score_total,
               data #>> '{{score,tier}}' AS tier, lead_id
        FROM import_rows WHERE {condition} ORDER BY row_number LIMIT :limit OFFSET :offset
        """,
        {"i": import_id, "limit": paging.limit, "offset": paging.offset},
    )
    return Page(items=[ImportRowOut(**r) for r in rows], total=total, limit=paging.limit, offset=paging.offset)


@router.patch(
    "/imports/{import_id}/rows/{row_id}", response_model=ImportRowOut, operation_id="updateImportRow", tags=["imports"]
)
def update_import_row(
    import_id: UUID, row_id: UUID, body: ImportRowUpdate, ctx: TenantContext = IMPORT
) -> ImportRowOut:
    """Decide what happens to one row, for example whether a possible duplicate is imported."""
    current = _import(ctx, import_id)
    if current.status != "ready":
        raise ConflictError("This import can no longer be changed.")
    row = one(
        ctx,
        "SELECT issues, duplicate_lead_id FROM import_rows WHERE tenant_id = :tenant_id AND import_id = :i AND id = :r FOR UPDATE",
        {"i": import_id, "r": row_id},
        "Row not found.",
    )
    if body.action != "skip" and any(issue["severity"] == "error" for issue in row["issues"]):
        raise ValidationFailed("A row with errors cannot be imported.")
    if body.action == "update" and row["duplicate_lead_id"] is None:
        raise ValidationFailed("There is no existing lead to update.")
    ctx.db.execute(
        text("UPDATE import_rows SET action = :a WHERE tenant_id = :t AND id = :r"),
        {"a": body.action, "t": ctx.tenant_id, "r": row_id},
    )
    return _row(ctx, import_id, row_id)


def _row(ctx: TenantContext, import_id: UUID, row_id: UUID) -> ImportRowOut:
    return ImportRowOut(
        **one(
            ctx,
            """
            SELECT id, row_number, external_id, data #>> '{company,name}' AS business_name, action, issues,
                   duplicate_lead_id, duplicate_reason, (data #>> '{score,total}')::int AS score_total,
                   data #>> '{score,tier}' AS tier, lead_id
            FROM import_rows WHERE tenant_id = :tenant_id AND import_id = :i AND id = :r
            """,
            {"i": import_id, "r": row_id},
            "Row not found.",
        )
    )


@router.put(
    "/imports/{import_id}/mapping",
    response_model=ImportOut,
    status_code=202,
    operation_id="updateImportMapping",
    tags=["imports"],
)
def update_import_mapping(import_id: UUID, body: MappingUpdate, ctx: TenantContext = IMPORT) -> ImportOut:
    """Change which column feeds which field, then rebuild the preview."""
    current = _import(ctx, import_id)
    if current.status not in ("ready", "failed"):
        raise ConflictError("This import can no longer be changed.")
    headers = set(current.report.get("headers", []))
    for field, header in body.mapping.items():
        if field not in FIELDS:
            raise ValidationFailed(f"Unknown field: {field}")
        if header not in headers:
            raise ValidationFailed(f"The file has no column called {header}.")
    ctx.db.execute(
        text(
            "UPDATE imports SET mapping = jsonb_build_object('override', CAST(:m AS jsonb)) WHERE tenant_id = :t AND id = :i"
        ),
        {"m": json.dumps(body.mapping), "t": ctx.tenant_id, "i": import_id},
    )
    ctx.db.commit()
    parse_import.delay(str(ctx.tenant_id), str(import_id), str(ctx.user_id))
    return _import(ctx, import_id)


@router.post(
    "/imports/{import_id}/commit",
    response_model=ImportOut,
    status_code=202,
    operation_id="commitImport",
    tags=["imports"],
)
def commit_import_endpoint(import_id: UUID, ctx: TenantContext = IMPORT) -> ImportOut:
    current = _import(ctx, import_id)
    if current.status != "ready":
        raise ConflictError("Only a reviewed import that is ready can be committed.")
    ctx.db.commit()
    commit_import_task.delay(str(ctx.tenant_id), str(import_id), str(ctx.user_id))
    return _import(ctx, import_id)


# --- export ----------------------------------------------------------------------------------


@router.get("/exports/prospects.xlsx", operation_id="exportProspects", tags=["exports"])
def export_prospects(ctx: TenantContext = tenant_with(Permission.EXPORT_RUN)) -> Response:
    tenant_name = scalar(ctx, "SELECT name FROM tenants WHERE id = :tenant_id")
    data, summary = build_export(ctx.db, ctx.tenant_id, tenant_name)
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="export.prospects",
        target_type="export",
        data={"rows": summary["total"], "bytes": len(data)},
    )
    return Response(
        content=data,
        media_type=XLSX_TYPE,
        headers={
            "Content-Disposition": 'attachment; filename="prospects.xlsx"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


# --- rubric and scores -----------------------------------------------------------------------


@router.get("/rubric", response_model=RubricOut, operation_id="getRubric", tags=["scoring"])
def get_rubric(ctx: TenantContext = READ) -> RubricOut:
    return RubricOut(
        **one(ctx, "SELECT * FROM rubric_versions WHERE tenant_id = :tenant_id AND is_active", {}, "No active rubric.")
    )


def _score_rows(ctx: TenantContext, lead_id: UUID, *, current_only: bool = False) -> list[ScoreOut]:
    rows = many(
        ctx,
        "SELECT s.*, r.version AS rubric_version FROM lead_scores s "
        "JOIN rubric_versions r ON r.tenant_id = s.tenant_id AND r.id = s.rubric_version_id "
        "WHERE s.tenant_id = :tenant_id AND s.lead_id = :l "
        + ("AND s.is_current " if current_only else "")
        + "ORDER BY s.created_at DESC, s.id DESC",
        {"l": lead_id},
    )
    return [ScoreOut(**r) for r in rows]


@router.get("/leads/{lead_id}/scores", response_model=list[ScoreOut], operation_id="listLeadScores", tags=["scoring"])
def list_lead_scores(lead_id: UUID, ctx: TenantContext = READ) -> list[ScoreOut]:
    """Score history, newest first. Each entry names the rubric version it was computed under."""
    exists(ctx, "leads", lead_id, "The lead")
    return _score_rows(ctx, lead_id)


@router.post(
    "/leads/{lead_id}/scores",
    response_model=ScoreOut,
    status_code=201,
    operation_id="overrideLeadScore",
    tags=["scoring"],
)
def override_lead_score(
    lead_id: UUID, body: ScoreIn, ctx: TenantContext = tenant_with(Permission.RESEARCH_REVIEW)
) -> ScoreOut:
    """A reviewer sets the components. The total and tier are computed here, never supplied."""
    lead = one(
        ctx,
        "SELECT id, company_id FROM leads WHERE tenant_id = :tenant_id AND id = :id FOR UPDATE",
        {"id": lead_id},
        "Lead not found.",
    )
    rubric_id, rubric = active_rubric(ctx.db, ctx.tenant_id)
    try:
        score = compute_score(dict(body.components), rubric)
    except ScoreError as exc:
        raise ValidationFailed(str(exc)) from exc
    if score is None:
        raise ValidationFailed("Give a value for every component.")
    previous = _score_rows(ctx, lead_id, current_only=True)
    ctx.db.execute(
        text("UPDATE lead_scores SET is_current = false WHERE tenant_id = :t AND lead_id = :l AND is_current"),
        {"t": ctx.tenant_id, "l": lead_id},
    )
    ctx.db.execute(
        text(
            "INSERT INTO lead_scores (tenant_id, lead_id, rubric_version_id, components, total, tier, origin, reasons, "
            "override_reason, created_by) VALUES (:t, :l, :r, CAST(:c AS jsonb), :total, :tier, 'override', "
            "CAST(:reasons AS jsonb), :why, :u)"
        ),
        {
            "t": ctx.tenant_id,
            "l": lead_id,
            "r": rubric_id,
            "c": json.dumps(score.components),
            "total": score.total,
            "tier": score.tier,
            "reasons": json.dumps(body.component_reasons),
            "why": body.reason,
            "u": ctx.user_id,
        },
    )
    before = f"{previous[0].total} ({previous[0].tier})" if previous else "unscored"
    log_activity(
        ctx,
        "lead.score_overridden",
        f"Score changed from {before} to {score.total} ({score.tier}): {body.reason}",
        company_id=lead["company_id"],
        lead_id=lead_id,
    )
    return _score_rows(ctx, lead_id, current_only=True)[0]
