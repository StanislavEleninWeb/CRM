"""Companies, contacts, contact channels, restrictions, duplicates and merging."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError

from app.core.deps import TenantContext, tenant_with
from app.core.errors import ConflictError, NotFoundError
from app.core.normalize import domain_of, normalize_email, normalize_phone, normalize_url
from app.core.pagination import Page, PageParams, page_params
from app.modules.crm.common import (
    ValidationFailed,
    check_owner,
    execute,
    exists,
    log_activity,
    many,
    one,
    order_by,
    scalar,
    set_clause,
    validate_custom,
)
from app.modules.crm.schemas import (
    BulkCompanies,
    BulkResult,
    ChannelIn,
    ChannelOut,
    ChannelUpdate,
    CompanyDetail,
    CompanyIn,
    CompanyOut,
    CompanyUpdate,
    ContactIn,
    ContactOut,
    DuplicateCandidate,
    MergeRequest,
    RestrictionIn,
    RestrictionLift,
    RestrictionOut,
)
from app.modules.identity.audit import record_audit
from app.modules.identity.permissions import Permission

router = APIRouter(tags=["companies"])
Paging = Annotated[PageParams, Depends(page_params)]
READ = tenant_with(Permission.CRM_READ)
WRITE = tenant_with(Permission.CRM_WRITE)

COMPANY_SORTS = {
    "name": "lower(c.name)",
    "created_at": "c.created_at",
    "updated_at": "c.updated_at",
    "next_action_at": "c.next_action_at",
    "city": "lower(c.city)",
}
CHANNEL_COLUMNS = """
    id, company_id, contact_id, kind, purpose, raw_value, normalized_value, normalized_is_e164,
    label, source_type, source_url, source_date, verification_state, do_not_contact,
    restriction_reason, allow_sales_use
"""


def _company(ctx: TenantContext, company_id: UUID, *, lock: bool = False) -> RowMapping:
    return one(
        ctx,
        f"SELECT * FROM companies WHERE tenant_id = :tenant_id AND id = :id {'FOR UPDATE' if lock else ''}",  # noqa: S608
        {"id": company_id},
        "Company not found.",
    )


def _website(raw: str | None) -> tuple[str | None, str | None]:
    if raw is None or raw == "":
        return None, None
    normalized = normalize_url(raw)
    if normalized is None:
        raise ValidationFailed("Enter a valid website address.")
    return normalized, domain_of(normalized)


def active_restricted_kinds(ctx: TenantContext, company_id: UUID) -> set[str]:
    return {
        row["channel_kind"]
        for row in many(
            ctx,
            "SELECT DISTINCT channel_kind FROM contact_restrictions "
            "WHERE tenant_id = :tenant_id AND company_id = :c AND lifted_at IS NULL",
            {"c": company_id},
        )
    }


def channel_out(row: RowMapping, restricted_kinds: set[str]) -> ChannelOut:
    """Attach a ``tel:`` link only when the number may be used for a sales call."""
    data = dict(row)
    blocked = "any" in restricted_kinds or data["kind"] in restricted_kinds
    usable = data["allow_sales_use"] and not blocked and data["verification_state"] != "invalid"
    data["allow_sales_use"] = usable
    dial = None
    if data["kind"] == "phone" and usable and data["normalized_value"]:
        dial = f"tel:{data['normalized_value']}"
    return ChannelOut(**data, dial_uri=dial)


@router.get("/companies", response_model=Page[CompanyOut], operation_id="listCompanies")
def list_companies(
    paging: Paging,
    ctx: TenantContext = READ,
    q: Annotated[str | None, Query(max_length=200)] = None,
    city: Annotated[str | None, Query(max_length=200)] = None,
    country: Annotated[str | None, Query(max_length=200)] = None,
    industry_group: Annotated[str | None, Query(max_length=200)] = None,
    owner_user_id: UUID | None = None,
    tag: Annotated[str | None, Query(max_length=120)] = None,
    archived: bool = False,
    sort: Annotated[str | None, Query(max_length=40)] = None,
) -> Page[CompanyOut]:
    where = ["c.tenant_id = :tenant_id", "c.merged_into_id IS NULL"]
    params: dict[str, Any] = {}
    where.append("c.archived_at IS NOT NULL" if archived else "c.archived_at IS NULL")
    if q:
        where.append("(lower(c.name) LIKE :q OR c.domain LIKE :q OR c.external_id ILIKE :q_exact)")
        escaped = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params |= {"q": f"%{escaped}%", "q_exact": q}
    for column, value in (("city", city), ("country", country), ("industry_group", industry_group)):
        if value:
            where.append(f"lower(c.{column}) = lower(:{column})")
            params[column] = value
    if owner_user_id:
        where.append("c.owner_user_id = :owner")
        params["owner"] = owner_user_id
    if tag:
        where.append(
            "EXISTS (SELECT 1 FROM taggings tg JOIN tags t ON t.tenant_id = tg.tenant_id AND t.id = tg.tag_id "
            "WHERE tg.tenant_id = c.tenant_id AND tg.entity_type = 'company' "
            "AND tg.entity_id = c.id AND t.name = :tag)"
        )
        params["tag"] = tag
    condition = " AND ".join(where)
    total = scalar(ctx, f"SELECT count(*) FROM companies c WHERE {condition}", params)  # noqa: S608
    ordering = order_by(sort, COMPANY_SORTS, "lower(c.name) ASC, c.id")
    items = many(
        ctx,
        f"SELECT c.* FROM companies c WHERE {condition} ORDER BY {ordering} LIMIT :limit OFFSET :offset",  # noqa: S608
        {**params, "limit": paging.limit, "offset": paging.offset},
    )
    return Page(items=[CompanyOut(**row) for row in items], total=total, limit=paging.limit, offset=paging.offset)


@router.post("/companies", response_model=CompanyOut, status_code=201, operation_id="createCompany")
def create_company(body: CompanyIn, ctx: TenantContext = WRITE) -> CompanyOut:
    check_owner(ctx, body.owner_user_id)
    website, domain = _website(body.website_url)
    try:
        row = one(
            ctx,
            """
            INSERT INTO companies (tenant_id, name, external_id, business_type, industry_group, city,
                                   country, language, website_url, domain, owner_user_id, next_action,
                                   next_action_at, custom)
            VALUES (:tenant_id, :name, :external_id, :business_type, :industry_group, :city, :country,
                    :language, :website_url, :domain, :owner_user_id, :next_action, :next_action_at,
                    CAST(:custom AS jsonb))
            RETURNING *
            """,
            {
                **body.model_dump(exclude={"custom", "website_url"}),
                "website_url": website,
                "domain": domain,
                "custom": validate_custom(ctx, "company", body.custom),
            },
            "Company not found.",
        )
    except IntegrityError as exc:
        raise ConflictError("A company with that external ID already exists.") from exc
    log_activity(ctx, "company.created", f"Company created: {row['name']}", company_id=row["id"])
    return CompanyOut(**row)


@router.get("/companies/{company_id}", response_model=CompanyDetail, operation_id="getCompany")
def get_company(company_id: UUID, ctx: TenantContext = READ) -> CompanyDetail:
    company = _company(ctx, company_id)
    restricted = active_restricted_kinds(ctx, company_id)
    scope = {"c": company_id}
    channels = many(
        ctx,
        f"SELECT {CHANNEL_COLUMNS} FROM contact_channels WHERE tenant_id = :tenant_id "  # noqa: S608
        "AND company_id = :c ORDER BY kind, position, created_at",
        scope,
    )
    contacts = many(
        ctx,
        "SELECT * FROM contacts WHERE tenant_id = :tenant_id AND company_id = :c "
        "AND archived_at IS NULL ORDER BY lower(full_name)",
        scope,
    )
    restrictions = many(
        ctx,
        "SELECT * FROM contact_restrictions WHERE tenant_id = :tenant_id AND company_id = :c ORDER BY created_at DESC",
        scope,
    )
    tags = many(
        ctx,
        "SELECT t.name FROM taggings tg JOIN tags t ON t.tenant_id = tg.tenant_id AND t.id = tg.tag_id "
        "WHERE tg.tenant_id = :tenant_id AND tg.entity_type = 'company' AND tg.entity_id = :c "
        "ORDER BY lower(t.name::text)",
        scope,
    )
    counts = one(
        ctx,
        """
        SELECT
          (SELECT count(*) FROM tasks WHERE tenant_id = :tenant_id AND company_id = :c AND status = 'open') AS open_task_count,
          (SELECT count(*) FROM leads WHERE tenant_id = :tenant_id AND company_id = :c) AS lead_count,
          (SELECT count(*) FROM deals WHERE tenant_id = :tenant_id AND company_id = :c) AS deal_count
        """,
        scope,
        "Company not found.",
    )
    return CompanyDetail(
        **company,
        channels=[channel_out(row, restricted) for row in channels],
        contacts=[ContactOut(**row) for row in contacts],
        restrictions=[RestrictionOut(**row) for row in restrictions],
        tags=[row["name"] for row in tags],
        **counts,
    )


@router.patch("/companies/{company_id}", response_model=CompanyOut, operation_id="updateCompany")
def update_company(company_id: UUID, body: CompanyUpdate, ctx: TenantContext = WRITE) -> CompanyOut:
    current = _company(ctx, company_id, lock=True)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("name", "x") is None:
        raise ValidationFailed("A company needs a name.")
    if "owner_user_id" in changes and changes["owner_user_id"] != current["owner_user_id"]:
        check_owner(ctx, changes["owner_user_id"])
    if "website_url" in changes:
        changes["website_url"], changes["domain"] = _website(changes["website_url"])
    assignments: list[str] = []
    if "custom" in changes:
        changes["custom"] = validate_custom(ctx, "company", changes["custom"])
        assignments.append("custom = CAST(:custom AS jsonb)")
    if "archived" in changes:
        archived = changes.pop("archived")
        assignments.append("archived_at = " + ("COALESCE(archived_at, now())" if archived else "NULL"))
    plain = {k: v for k, v in changes.items() if k != "custom"}
    if plain:
        assignments.append(set_clause(plain))
    if not assignments:
        return CompanyOut(**current)
    try:
        row = one(
            ctx,
            f"UPDATE companies SET {', '.join(assignments)} WHERE tenant_id = :tenant_id AND id = :id RETURNING *",  # noqa: S608
            {**changes, "id": company_id},
            "Company not found.",
        )
    except IntegrityError as exc:
        raise ConflictError("A company with that external ID already exists.") from exc
    log_activity(
        ctx,
        "company.updated",
        "Company details updated",
        company_id=company_id,
        data={"fields": sorted(body.model_dump(exclude_unset=True))},
    )
    return CompanyOut(**row)


@router.delete("/companies/{company_id}", status_code=204, operation_id="deleteCompany")
def delete_company(company_id: UUID, ctx: TenantContext = tenant_with(Permission.CRM_DELETE)) -> None:
    company = _company(ctx, company_id, lock=True)
    execute(ctx, "DELETE FROM companies WHERE tenant_id = :tenant_id AND id = :id", {"id": company_id})
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="company.deleted",
        target_type="company",
        target_id=str(company_id),
        data={"name": company["name"]},
    )


@router.post("/companies/bulk", response_model=BulkResult, operation_id="bulkCompanies")
def bulk_companies(body: BulkCompanies, ctx: TenantContext = tenant_with(Permission.CRM_BULK)) -> BulkResult:
    """Preview with ``dry_run`` (the default), then repeat with ``dry_run: false`` to apply."""
    ids = list(dict.fromkeys(body.ids))
    found = {
        row["id"]
        for row in many(
            ctx,
            "SELECT id FROM companies WHERE tenant_id = :tenant_id AND id = ANY(:ids) FOR UPDATE",
            {"ids": ids},
        )
    }
    missing = [i for i in ids if i not in found]
    if body.dry_run or not found:
        return BulkResult(matched=len(found), changed=0, dry_run=body.dry_run, missing=missing)
    targets = {"ids": list(found)}
    if body.action == "assign_owner":
        check_owner(ctx, body.owner_user_id)
        changed = execute(
            ctx,
            "UPDATE companies SET owner_user_id = :o WHERE tenant_id = :tenant_id AND id = ANY(:ids)",
            {"o": body.owner_user_id, **targets},
        )
    elif body.action in ("archive", "unarchive"):
        value = "COALESCE(archived_at, now())" if body.action == "archive" else "NULL"
        changed = execute(
            ctx,
            f"UPDATE companies SET archived_at = {value} WHERE tenant_id = :tenant_id AND id = ANY(:ids)",  # noqa: S608
            targets,
        )
    else:
        tag_id = _tag_id(ctx, body.tag or "")
        changed = execute(
            ctx,
            "INSERT INTO taggings (tenant_id, tag_id, entity_type, entity_id) "
            "SELECT :tenant_id, :tag, 'company', unnest(CAST(:ids AS uuid[])) ON CONFLICT DO NOTHING",
            {"tag": tag_id, **targets},
        )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action=f"company.bulk_{body.action}",
        target_type="company",
        data={"count": len(found)},
    )
    return BulkResult(matched=len(found), changed=changed, dry_run=False, missing=missing)


def _tag_id(ctx: TenantContext, name: str) -> UUID:
    tag_id: UUID = scalar(
        ctx,
        "INSERT INTO tags (tenant_id, name) VALUES (:tenant_id, :n) "
        "ON CONFLICT (tenant_id, name) DO UPDATE SET name = tags.name RETURNING id",
        {"n": name},
    )
    return tag_id


# --- contacts --------------------------------------------------------------------------------


@router.post(
    "/companies/{company_id}/contacts", response_model=ContactOut, status_code=201, operation_id="createContact"
)
def create_contact(company_id: UUID, body: ContactIn, ctx: TenantContext = WRITE) -> ContactOut:
    _company(ctx, company_id)
    row = one(
        ctx,
        "INSERT INTO contacts (tenant_id, company_id, full_name, job_title, language) "
        "VALUES (:tenant_id, :c, :full_name, :job_title, :language) RETURNING *",
        {"c": company_id, **body.model_dump()},
        "Contact not found.",
    )
    log_activity(
        ctx, "contact.created", f"Contact added: {row['full_name']}", company_id=company_id, contact_id=row["id"]
    )
    return ContactOut(**row)


@router.patch("/contacts/{contact_id}", response_model=ContactOut, operation_id="updateContact")
def update_contact(contact_id: UUID, body: ContactIn, ctx: TenantContext = WRITE) -> ContactOut:
    row = one(
        ctx,
        "UPDATE contacts SET full_name = :full_name, job_title = :job_title, language = :language "
        "WHERE tenant_id = :tenant_id AND id = :id RETURNING *",
        {"id": contact_id, **body.model_dump()},
        "Contact not found.",
    )
    return ContactOut(**row)


@router.delete("/contacts/{contact_id}", status_code=204, operation_id="archiveContact")
def archive_contact(contact_id: UUID, ctx: TenantContext = WRITE) -> None:
    one(
        ctx,
        "UPDATE contacts SET archived_at = now() WHERE tenant_id = :tenant_id AND id = :id RETURNING id",
        {"id": contact_id},
        "Contact not found.",
    )


# --- channels --------------------------------------------------------------------------------


def normalize_channel(kind: str, raw: str, country: str | None) -> tuple[str | None, bool]:
    if kind == "phone":
        return normalize_phone(raw, country)
    if kind == "email":
        return normalize_email(raw), False
    if kind in ("website", "contact_page"):
        return normalize_url(raw), False
    return raw.strip().lower() or None, False


@router.post(
    "/companies/{company_id}/channels", response_model=ChannelOut, status_code=201, operation_id="createChannel"
)
def create_channel(company_id: UUID, body: ChannelIn, ctx: TenantContext = WRITE) -> ChannelOut:
    company = _company(ctx, company_id)
    exists(ctx, "contacts", body.contact_id, "The contact")
    normalized, is_e164 = normalize_channel(body.kind, body.raw_value, company["country"])
    if body.kind == "email" and normalized is None:
        raise ValidationFailed("Enter a valid email address.")
    if body.kind == "phone" and normalized is None:
        raise ValidationFailed("Enter a valid phone number.")
    row = one(
        ctx,
        f"""
        INSERT INTO contact_channels (tenant_id, company_id, contact_id, kind, purpose, raw_value,
                                      normalized_value, normalized_is_e164, label, source_url,
                                      source_date, verification_state, position)
        VALUES (:tenant_id, :c, :contact_id, :kind, :purpose, :raw_value, :normalized, :is_e164,
                :label, :source_url, :source_date, :verification_state,
                (SELECT COALESCE(max(position), 0) + 1 FROM contact_channels
                 WHERE tenant_id = :tenant_id AND company_id = :c))
        RETURNING {CHANNEL_COLUMNS}
        """,  # noqa: S608
        {"c": company_id, "normalized": normalized, "is_e164": is_e164, **body.model_dump()},
        "Channel not found.",
    )
    log_activity(ctx, "channel.added", f"{body.kind.replace('_', ' ').capitalize()} added", company_id=company_id)
    return channel_out(row, active_restricted_kinds(ctx, company_id))


@router.patch("/channels/{channel_id}", response_model=ChannelOut, operation_id="updateChannel")
def update_channel(channel_id: UUID, body: ChannelUpdate, ctx: TenantContext = WRITE) -> ChannelOut:
    current = one(
        ctx,
        "SELECT * FROM contact_channels WHERE tenant_id = :tenant_id AND id = :id FOR UPDATE",
        {"id": channel_id},
        "Channel not found.",
    )
    changes = body.model_dump(exclude_unset=True)
    extra = ""
    if "do_not_contact" in changes and changes["do_not_contact"] != current["do_not_contact"]:
        if changes["do_not_contact"]:
            extra = ", restricted_at = now(), restricted_by = :actor"
        else:
            # Lifting a restriction is a manager decision.
            ctx.require(Permission.OUTREACH_APPROVE)
            changes["restriction_reason"] = None
            extra = ", restricted_at = NULL, restricted_by = NULL"
        record_audit(
            ctx.db,
            tenant_id=ctx.tenant_id,
            actor_id=ctx.user_id,
            action="channel.restricted" if changes["do_not_contact"] else "channel.restriction_lifted",
            target_type="contact_channel",
            target_id=str(channel_id),
            data={"reason": body.restriction_reason, "previous_reason": current["restriction_reason"]},
        )
        log_activity(
            ctx,
            "channel.restricted" if changes["do_not_contact"] else "channel.restriction_lifted",
            ("Marked do-not-contact: " + (body.restriction_reason or ""))
            if changes["do_not_contact"]
            else "Do-not-contact removed",
            company_id=current["company_id"],
        )
    elif "restriction_reason" in changes and not current["do_not_contact"]:
        changes.pop("restriction_reason")
    if not changes:
        return channel_out(current, active_restricted_kinds(ctx, current["company_id"]))
    row = one(
        ctx,
        f"UPDATE contact_channels SET {set_clause(changes)}{extra} "  # noqa: S608
        f"WHERE tenant_id = :tenant_id AND id = :id RETURNING {CHANNEL_COLUMNS}",
        {**changes, "id": channel_id, "actor": ctx.user_id},
        "Channel not found.",
    )
    return channel_out(row, active_restricted_kinds(ctx, row["company_id"]))


@router.delete("/channels/{channel_id}", status_code=204, operation_id="deleteChannel")
def delete_channel(channel_id: UUID, ctx: TenantContext = WRITE) -> None:
    current = one(
        ctx,
        "SELECT do_not_contact FROM contact_channels WHERE tenant_id = :tenant_id AND id = :id FOR UPDATE",
        {"id": channel_id},
        "Channel not found.",
    )
    if current["do_not_contact"]:
        # Deleting the row would erase the restriction and let a re-import bring the number back.
        raise ConflictError("A do-not-contact channel cannot be deleted.")
    execute(ctx, "DELETE FROM contact_channels WHERE tenant_id = :tenant_id AND id = :id", {"id": channel_id})


# --- company-level restrictions --------------------------------------------------------------


@router.post(
    "/companies/{company_id}/restrictions",
    response_model=RestrictionOut,
    status_code=201,
    operation_id="createRestriction",
)
def create_restriction(company_id: UUID, body: RestrictionIn, ctx: TenantContext = WRITE) -> RestrictionOut:
    _company(ctx, company_id)
    exists(ctx, "contacts", body.contact_id, "The contact")
    row = one(
        ctx,
        "INSERT INTO contact_restrictions (tenant_id, company_id, contact_id, channel_kind, reason, created_by) "
        "VALUES (:tenant_id, :c, :contact_id, :channel_kind, :reason, :actor) RETURNING *",
        {"c": company_id, "actor": ctx.user_id, **body.model_dump()},
        "Restriction not found.",
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="restriction.created",
        target_type="company",
        target_id=str(company_id),
        data={"channel_kind": body.channel_kind, "reason": body.reason},
    )
    log_activity(
        ctx,
        "restriction.created",
        f"Do not contact ({body.channel_kind}): {body.reason}",
        company_id=company_id,
    )
    return RestrictionOut(**row)


@router.post("/restrictions/{restriction_id}/lift", response_model=RestrictionOut, operation_id="liftRestriction")
def lift_restriction(
    restriction_id: UUID,
    body: RestrictionLift,
    ctx: TenantContext = tenant_with(Permission.OUTREACH_APPROVE),
) -> RestrictionOut:
    row = one(
        ctx,
        "UPDATE contact_restrictions SET lifted_at = now(), lifted_by = :actor, lift_reason = :r "
        "WHERE tenant_id = :tenant_id AND id = :id AND lifted_at IS NULL RETURNING *",
        {"id": restriction_id, "actor": ctx.user_id, "r": body.lift_reason},
        "Restriction not found or already lifted.",
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="restriction.lifted",
        target_type="company",
        target_id=str(row["company_id"]),
        data={"channel_kind": row["channel_kind"], "lift_reason": body.lift_reason},
    )
    log_activity(ctx, "restriction.lifted", f"Restriction lifted: {body.lift_reason}", company_id=row["company_id"])
    return RestrictionOut(**row)


# --- duplicates and merging ------------------------------------------------------------------


@router.get(
    "/companies/{company_id}/duplicates",
    response_model=list[DuplicateCandidate],
    operation_id="listCompanyDuplicates",
)
def list_duplicates(company_id: UUID, ctx: TenantContext = READ) -> list[DuplicateCandidate]:
    """Suggestions only. Nothing is merged without an explicit, reviewed request."""
    company = _company(ctx, company_id)
    rows = many(
        ctx,
        """
        WITH mine AS (
            SELECT kind, normalized_value FROM contact_channels
            WHERE tenant_id = :tenant_id AND company_id = :id
              AND kind IN ('phone', 'email') AND normalized_value IS NOT NULL
        )
        SELECT c.*,
               similarity(lower(c.name), lower(:name)) AS name_similarity,
               (c.domain IS NOT NULL AND c.domain = :domain) AS same_domain,
               EXISTS (SELECT 1 FROM contact_channels ch JOIN mine m
                       ON m.kind = ch.kind AND m.normalized_value = ch.normalized_value
                       WHERE ch.tenant_id = c.tenant_id AND ch.company_id = c.id AND ch.kind = 'phone') AS same_phone,
               EXISTS (SELECT 1 FROM contact_channels ch JOIN mine m
                       ON m.kind = ch.kind AND m.normalized_value = ch.normalized_value
                       WHERE ch.tenant_id = c.tenant_id AND ch.company_id = c.id AND ch.kind = 'email') AS same_email,
               (c.city IS NOT NULL AND lower(c.city) = lower(:city)) AS same_city
        FROM companies c
        WHERE c.tenant_id = :tenant_id AND c.id <> :id AND c.merged_into_id IS NULL
        ORDER BY name_similarity DESC, c.id
        LIMIT 500
        """,
        {"id": company_id, "name": company["name"], "domain": company["domain"], "city": company["city"] or ""},
    )
    candidates: list[DuplicateCandidate] = []
    for row in rows:
        data = dict(row)
        similarity = float(data.pop("name_similarity") or 0)
        flags = {key: data.pop(key) for key in ("same_domain", "same_phone", "same_email", "same_city")}
        reasons = [
            label
            for key, label in (
                ("same_domain", "Same website domain"),
                ("same_phone", "Shares a phone number"),
                ("same_email", "Shares an email address"),
            )
            if flags[key]
        ]
        if similarity >= 0.6 and flags["same_city"]:
            reasons.append("Similar name in the same city")
        if not reasons:
            continue
        caution = None
        if reasons == ["Same website domain"] and not flags["same_city"]:
            caution = "Same domain but a different city: this may be another branch, not a duplicate."
        candidates.append(
            DuplicateCandidate(
                company=CompanyOut(**data), reasons=reasons, name_similarity=round(similarity, 3), caution=caution
            )
        )
    return candidates[:25]


MOVABLE = ("contacts", "leads", "deals", "tasks", "notes", "activities", "attachments", "contact_restrictions")


@router.post("/companies/{company_id}/merge", response_model=CompanyDetail, operation_id="mergeCompanies")
def merge_companies(
    company_id: UUID, body: MergeRequest, ctx: TenantContext = tenant_with(Permission.CRM_MERGE)
) -> CompanyDetail:
    """Merge ``source_id`` into this company. History, restrictions and evidence all move."""
    if body.source_id == company_id:
        raise ValidationFailed("A company cannot be merged into itself.")
    # Lock in a stable order so two opposite merges cannot deadlock.
    first, second = sorted((company_id, body.source_id))
    locked = {first: _try(ctx, first), second: _try(ctx, second)}
    target, source = locked[company_id], locked[body.source_id]
    if target is None or source is None:
        raise NotFoundError("Company not found.")
    if source["merged_into_id"] is not None or target["merged_into_id"] is not None:
        raise ConflictError("One of these companies was already merged.")

    scope = {"t": ctx.tenant_id, "target": company_id, "source": body.source_id}
    moved: dict[str, int] = {}
    for table in MOVABLE:
        moved[table] = execute(
            ctx,
            f"UPDATE {table} SET company_id = :target WHERE tenant_id = :t AND company_id = :source",  # noqa: S608
            scope,
        )
    # A channel that exists on both keeps the stricter state: a restriction always survives.
    ctx.db.execute(
        text(
            """
            UPDATE contact_channels keep SET
                do_not_contact = true,
                restriction_reason = COALESCE(keep.restriction_reason, dup.restriction_reason),
                restricted_at = COALESCE(keep.restricted_at, dup.restricted_at),
                restricted_by = COALESCE(keep.restricted_by, dup.restricted_by)
            FROM contact_channels dup
            WHERE keep.tenant_id = :t AND keep.company_id = :target
              AND dup.tenant_id = :t AND dup.company_id = :source
              AND dup.kind = keep.kind AND dup.normalized_value = keep.normalized_value
              AND dup.do_not_contact
            """
        ),
        scope,
    )
    ctx.db.execute(
        text(
            """
            DELETE FROM contact_channels dup USING contact_channels keep
            WHERE dup.tenant_id = :t AND dup.company_id = :source
              AND keep.tenant_id = :t AND keep.company_id = :target
              AND dup.kind = keep.kind AND dup.normalized_value = keep.normalized_value
            """
        ),
        scope,
    )
    moved["contact_channels"] = execute(
        ctx, "UPDATE contact_channels SET company_id = :target WHERE tenant_id = :t AND company_id = :source", scope
    )
    ctx.db.execute(
        text(
            """
            INSERT INTO taggings (tenant_id, tag_id, entity_type, entity_id, origin)
            SELECT tenant_id, tag_id, 'company', :target, origin FROM taggings
            WHERE tenant_id = :t AND entity_type = 'company' AND entity_id = :source
            ON CONFLICT DO NOTHING
            """
        ),
        scope,
    )
    ctx.db.execute(
        text("DELETE FROM taggings WHERE tenant_id = :t AND entity_type = 'company' AND entity_id = :source"),
        scope,
    )
    # Fill blanks on the survivor from the merged record; never overwrite existing values.
    ctx.db.execute(
        text(
            """
            UPDATE companies t SET
                business_type = COALESCE(t.business_type, s.business_type),
                industry_group = COALESCE(t.industry_group, s.industry_group),
                city = COALESCE(t.city, s.city),
                country = COALESCE(t.country, s.country),
                language = COALESCE(t.language, s.language),
                website_url = COALESCE(t.website_url, s.website_url),
                domain = COALESCE(t.domain, s.domain),
                owner_user_id = COALESCE(t.owner_user_id, s.owner_user_id)
            FROM companies s
            WHERE t.tenant_id = :t AND t.id = :target AND s.tenant_id = :t AND s.id = :source
            """
        ),
        scope,
    )
    ctx.db.execute(
        text(
            "UPDATE companies SET merged_into_id = :target, archived_at = COALESCE(archived_at, now()) "
            "WHERE tenant_id = :t AND id = :source"
        ),
        scope,
    )
    log_activity(
        ctx,
        "company.merged",
        f"Merged {source['name']} into this company",
        company_id=company_id,
        data={"source_id": str(body.source_id), "source_name": source["name"], "moved": moved},
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="company.merged",
        target_type="company",
        target_id=str(company_id),
        data={"source_id": str(body.source_id), "moved": moved},
    )
    return get_company(company_id, ctx)


def _try(ctx: TenantContext, company_id: UUID) -> RowMapping | None:
    try:
        return _company(ctx, company_id, lock=True)
    except NotFoundError:
        return None
