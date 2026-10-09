"""Notes, tags, the activity timeline, attachments, saved views and custom fields."""

from collections.abc import Iterator
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.deps import TenantContext, tenant_with
from app.core.errors import AppError, ConflictError
from app.core.pagination import Page, PageParams, page_params
from app.core.storage import (
    ALLOWED_CONTENT_TYPES,
    MAX_UPLOAD_BYTES,
    Storage,
    get_storage,
    new_storage_key,
    safe_filename,
    sha256_of,
)
from app.modules.crm.common import (
    ENTITY_TABLES,
    MAX_CUSTOM_FIELDS_PER_ENTITY,
    ValidationFailed,
    exists,
    log_activity,
    many,
    one,
    scalar,
)
from app.modules.crm.schemas import (
    ActivityOut,
    AttachmentOut,
    CustomFieldIn,
    CustomFieldOut,
    EntityType,
    NoteIn,
    NoteOut,
    SavedViewIn,
    SavedViewOut,
    TagsIn,
)
from app.modules.identity.permissions import Permission

router = APIRouter()
Paging = Annotated[PageParams, Depends(page_params)]
READ = tenant_with(Permission.CRM_READ)
WRITE = tenant_with(Permission.CRM_WRITE)
LINK_COLUMNS = ("company_id", "contact_id", "lead_id", "deal_id")
LINK_TABLES = {"company_id": "companies", "contact_id": "contacts", "lead_id": "leads", "deal_id": "deals"}


class PayloadTooLarge(AppError):
    status_code = 413
    code = "payload_too_large"


def _check_links(ctx: TenantContext, links: dict[str, UUID | None]) -> None:
    for column, value in links.items():
        exists(ctx, LINK_TABLES[column], value, "The linked record")


def _link_filter(links: dict[str, UUID | None], alias: str) -> tuple[str, dict[str, Any]]:
    given = {k: v for k, v in links.items() if v is not None}
    if not given:
        raise ValidationFailed("Specify which record to list for.")
    clause = " AND ".join(f"{alias}.{column} = :{column}" for column in given)
    return clause, given


# --- notes -----------------------------------------------------------------------------------


@router.post("/notes", response_model=NoteOut, status_code=201, operation_id="createNote", tags=["notes"])
def create_note(body: NoteIn, ctx: TenantContext = WRITE) -> NoteOut:
    links = body.model_dump(include=set(LINK_COLUMNS))
    _check_links(ctx, links)
    row = one(
        ctx,
        "INSERT INTO notes (tenant_id, body, author_user_id, company_id, contact_id, lead_id, deal_id) "
        "VALUES (:tenant_id, :body, :actor, :company_id, :contact_id, :lead_id, :deal_id) RETURNING *",
        {"body": body.body, "actor": ctx.user_id, **links},
        "Note not found.",
    )
    preview = body.body if len(body.body) <= 140 else body.body[:137] + "…"
    log_activity(
        ctx,
        "note.added",
        preview,
        data={"note_id": str(row["id"])},
        company_id=body.company_id,
        contact_id=body.contact_id,
        lead_id=body.lead_id,
        deal_id=body.deal_id,
    )
    return NoteOut(**row)


@router.get("/notes", response_model=Page[NoteOut], operation_id="listNotes", tags=["notes"])
def list_notes(
    paging: Paging,
    ctx: TenantContext = READ,
    company_id: UUID | None = None,
    lead_id: UUID | None = None,
    deal_id: UUID | None = None,
) -> Page[NoteOut]:
    clause, params = _link_filter({"company_id": company_id, "lead_id": lead_id, "deal_id": deal_id}, "n")
    total = scalar(ctx, f"SELECT count(*) FROM notes n WHERE n.tenant_id = :tenant_id AND {clause}", params)
    rows = many(
        ctx,
        f"SELECT n.* FROM notes n WHERE n.tenant_id = :tenant_id AND {clause} "
        "ORDER BY n.created_at DESC, n.id LIMIT :limit OFFSET :offset",
        {**params, "limit": paging.limit, "offset": paging.offset},
    )
    return Page(items=[NoteOut(**row) for row in rows], total=total, limit=paging.limit, offset=paging.offset)


# --- tags ------------------------------------------------------------------------------------


@router.get("/tags", response_model=list[str], operation_id="listTags", tags=["tags"])
def list_tags(ctx: TenantContext = READ) -> list[str]:
    return [
        row["name"]
        for row in many(ctx, "SELECT name FROM tags WHERE tenant_id = :tenant_id ORDER BY lower(name::text) LIMIT 1000")
    ]


@router.put("/{entity_type}/{entity_id}/tags", response_model=list[str], operation_id="setTags", tags=["tags"])
def set_tags(entity_type: EntityType, entity_id: UUID, body: TagsIn, ctx: TenantContext = WRITE) -> list[str]:
    """Replace the manual tags on a record. Imported and automated tags are kept."""
    exists(ctx, ENTITY_TABLES[entity_type], entity_id, "The record")
    scope = {"t": ctx.tenant_id, "e": entity_type, "id": entity_id}
    ctx.db.execute(
        text(
            "DELETE FROM taggings WHERE tenant_id = :t AND entity_type = :e AND entity_id = :id AND origin = 'manual'"
        ),
        scope,
    )
    for name in body.tags:
        ctx.db.execute(
            text(
                """
                WITH tag AS (
                    INSERT INTO tags (tenant_id, name) VALUES (:t, :n)
                    ON CONFLICT (tenant_id, name) DO UPDATE SET name = tags.name RETURNING id
                )
                INSERT INTO taggings (tenant_id, tag_id, entity_type, entity_id)
                SELECT :t, id, :e, :id FROM tag ON CONFLICT DO NOTHING
                """
            ),
            {**scope, "n": name},
        )
    rows = many(
        ctx,
        "SELECT t.name FROM taggings tg JOIN tags t ON t.tenant_id = tg.tenant_id AND t.id = tg.tag_id "
        "WHERE tg.tenant_id = :tenant_id AND tg.entity_type = :e AND tg.entity_id = :id ORDER BY lower(t.name::text)",
        {"e": entity_type, "id": entity_id},
    )
    return [row["name"] for row in rows]


# --- activities ------------------------------------------------------------------------------


@router.get("/activities", response_model=Page[ActivityOut], operation_id="listActivities", tags=["activities"])
def list_activities(
    paging: Paging,
    ctx: TenantContext = READ,
    company_id: UUID | None = None,
    lead_id: UUID | None = None,
    deal_id: UUID | None = None,
    kind: Annotated[str | None, Query(max_length=60)] = None,
) -> Page[ActivityOut]:
    clause, params = _link_filter({"company_id": company_id, "lead_id": lead_id, "deal_id": deal_id}, "a")
    if kind:
        clause += " AND a.kind = :kind"
        params["kind"] = kind
    total = scalar(ctx, f"SELECT count(*) FROM activities a WHERE a.tenant_id = :tenant_id AND {clause}", params)
    rows = many(
        ctx,
        f"SELECT a.* FROM activities a WHERE a.tenant_id = :tenant_id AND {clause} "
        "ORDER BY a.occurred_at DESC, a.id DESC LIMIT :limit OFFSET :offset",
        {**params, "limit": paging.limit, "offset": paging.offset},
    )
    return Page(items=[ActivityOut(**row) for row in rows], total=total, limit=paging.limit, offset=paging.offset)


# --- attachments -----------------------------------------------------------------------------


@router.post(
    "/attachments", response_model=AttachmentOut, status_code=201, operation_id="uploadAttachment", tags=["attachments"]
)
def upload_attachment(
    file: Annotated[UploadFile, File()],
    storage: Annotated[Storage, Depends(get_storage)],
    ctx: TenantContext = WRITE,
    company_id: Annotated[UUID | None, Form()] = None,
    lead_id: Annotated[UUID | None, Form()] = None,
    deal_id: Annotated[UUID | None, Form()] = None,
) -> AttachmentOut:
    links: dict[str, UUID | None] = {"company_id": company_id, "lead_id": lead_id, "deal_id": deal_id}
    if not any(links.values()):
        raise ValidationFailed("Attach the file to a company, lead or opportunity.")
    _check_links(ctx, links)
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ValidationFailed("This file type is not allowed.")
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise PayloadTooLarge(f"Files are limited to {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    if not data:
        raise ValidationFailed("The file is empty.")
    filename = safe_filename(file.filename or "file")
    key = new_storage_key(ctx.tenant_id)
    storage.put(key, data, content_type)
    row = one(
        ctx,
        """
        INSERT INTO attachments (tenant_id, filename, content_type, size_bytes, sha256, storage_key,
                                 uploaded_by, company_id, lead_id, deal_id)
        VALUES (:tenant_id, :filename, :content_type, :size, :sha, :key, :actor, :company_id, :lead_id, :deal_id)
        RETURNING *
        """,
        {
            "filename": filename,
            "content_type": content_type,
            "size": len(data),
            "sha": sha256_of(data),
            "key": key,
            "actor": ctx.user_id,
            **links,
        },
        "Attachment not found.",
    )
    log_activity(ctx, "file.added", f"File added: {filename}", company_id=company_id, lead_id=lead_id, deal_id=deal_id)
    return AttachmentOut(**row)


@router.get("/attachments", response_model=list[AttachmentOut], operation_id="listAttachments", tags=["attachments"])
def list_attachments(
    ctx: TenantContext = READ,
    company_id: UUID | None = None,
    lead_id: UUID | None = None,
    deal_id: UUID | None = None,
) -> list[AttachmentOut]:
    clause, params = _link_filter({"company_id": company_id, "lead_id": lead_id, "deal_id": deal_id}, "f")
    rows = many(
        ctx,
        f"SELECT f.* FROM attachments f WHERE f.tenant_id = :tenant_id AND f.deleted_at IS NULL AND {clause} "
        "ORDER BY f.created_at DESC LIMIT 200",
        params,
    )
    return [AttachmentOut(**row) for row in rows]


@router.get("/attachments/{attachment_id}/download", operation_id="downloadAttachment", tags=["attachments"])
def download_attachment(
    attachment_id: UUID, storage: Annotated[Storage, Depends(get_storage)], ctx: TenantContext = READ
) -> StreamingResponse:
    """Authorised on every request and streamed by the API: there is no shareable storage URL."""
    row = one(
        ctx,
        "SELECT filename, content_type, size_bytes, storage_key FROM attachments "
        "WHERE tenant_id = :tenant_id AND id = :id AND deleted_at IS NULL",
        {"id": attachment_id},
        "File not found.",
    )
    key: str = row["storage_key"]
    if not key.startswith(f"{ctx.tenant_id}/"):
        raise ConflictError("This file cannot be served.")
    chunks: Iterator[bytes] = storage.iter_chunks(key)
    return StreamingResponse(
        chunks,
        media_type=row["content_type"],
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row['filename'])}",
            "Content-Length": str(row["size_bytes"]),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.delete("/attachments/{attachment_id}", status_code=204, operation_id="deleteAttachment", tags=["attachments"])
def delete_attachment(
    attachment_id: UUID, storage: Annotated[Storage, Depends(get_storage)], ctx: TenantContext = WRITE
) -> None:
    row = one(
        ctx,
        "UPDATE attachments SET deleted_at = now() WHERE tenant_id = :tenant_id AND id = :id "
        "AND deleted_at IS NULL RETURNING storage_key, filename, company_id, lead_id, deal_id",
        {"id": attachment_id},
        "File not found.",
    )
    storage.delete(row["storage_key"])
    log_activity(
        ctx,
        "file.removed",
        f"File removed: {row['filename']}",
        company_id=row["company_id"],
        lead_id=row["lead_id"],
        deal_id=row["deal_id"],
    )


# --- saved views -----------------------------------------------------------------------------


@router.get("/saved-views", response_model=list[SavedViewOut], operation_id="listSavedViews", tags=["views"])
def list_saved_views(entity_type: str, ctx: TenantContext = READ) -> list[SavedViewOut]:
    rows = many(
        ctx,
        "SELECT * FROM saved_views WHERE tenant_id = :tenant_id AND entity_type = :e "
        "AND (is_shared OR owner_user_id = :u) ORDER BY lower(name)",
        {"e": entity_type, "u": ctx.user_id},
    )
    return [SavedViewOut(**row) for row in rows]


@router.post(
    "/saved-views", response_model=SavedViewOut, status_code=201, operation_id="createSavedView", tags=["views"]
)
def create_saved_view(body: SavedViewIn, ctx: TenantContext = READ) -> SavedViewOut:
    if body.is_shared:
        ctx.require(Permission.CRM_BULK)  # shared views are a manager decision
    import json

    row = one(
        ctx,
        "INSERT INTO saved_views (tenant_id, entity_type, name, owner_user_id, is_shared, filters, sort) "
        "VALUES (:tenant_id, :entity_type, :name, :u, :is_shared, CAST(:filters AS jsonb), :sort) RETURNING *",
        {**body.model_dump(exclude={"filters"}), "filters": json.dumps(body.filters), "u": ctx.user_id},
        "View not found.",
    )
    return SavedViewOut(**row)


@router.delete("/saved-views/{view_id}", status_code=204, operation_id="deleteSavedView", tags=["views"])
def delete_saved_view(view_id: UUID, ctx: TenantContext = READ) -> None:
    view = one(
        ctx,
        "SELECT owner_user_id, is_shared FROM saved_views WHERE tenant_id = :tenant_id AND id = :id",
        {"id": view_id},
        "View not found.",
    )
    if view["owner_user_id"] != ctx.user_id:
        ctx.require(Permission.CRM_BULK)
    ctx.db.execute(
        text("DELETE FROM saved_views WHERE tenant_id = :t AND id = :id"), {"t": ctx.tenant_id, "id": view_id}
    )


# --- custom fields ---------------------------------------------------------------------------


@router.get(
    "/custom-fields", response_model=list[CustomFieldOut], operation_id="listCustomFields", tags=["custom fields"]
)
def list_custom_fields(ctx: TenantContext = READ) -> list[CustomFieldOut]:
    rows = many(
        ctx, "SELECT * FROM custom_field_definitions WHERE tenant_id = :tenant_id ORDER BY entity_type, lower(label)"
    )
    return [CustomFieldOut(**row) for row in rows]


@router.post(
    "/custom-fields",
    response_model=CustomFieldOut,
    status_code=201,
    operation_id="createCustomField",
    tags=["custom fields"],
)
def create_custom_field(
    body: CustomFieldIn, ctx: TenantContext = tenant_with(Permission.TENANT_SETTINGS)
) -> CustomFieldOut:
    import json

    count = scalar(
        ctx,
        "SELECT count(*) FROM custom_field_definitions WHERE tenant_id = :tenant_id AND entity_type = :e",
        {"e": body.entity_type},
    )
    if count >= MAX_CUSTOM_FIELDS_PER_ENTITY:
        raise ConflictError(f"Each record type is limited to {MAX_CUSTOM_FIELDS_PER_ENTITY} custom fields.")
    try:
        row = one(
            ctx,
            "INSERT INTO custom_field_definitions (tenant_id, entity_type, key, label, field_type, options) "
            "VALUES (:tenant_id, :entity_type, :key, :label, :field_type, CAST(:options AS jsonb)) RETURNING *",
            {**body.model_dump(exclude={"options"}), "options": json.dumps(body.options)},
            "Field not found.",
        )
    except IntegrityError as exc:
        raise ConflictError("A field with that key already exists.") from exc
    return CustomFieldOut(**row)
