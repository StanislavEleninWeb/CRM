"""Workspaces, members, invitations and the audit log."""

from datetime import timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.deps import CurrentPrincipal, Tenant, TenantContext, UserSession, tenant_with
from app.core.errors import AppError, ConflictError, NotFoundError, PermissionDeniedError
from app.core.pagination import Page, PageParams, page_params
from app.core.security import hash_token, new_token
from app.core.time import utcnow
from app.modules.identity.audit import record_audit
from app.modules.identity.permissions import Permission, Role, can_assign_role
from app.modules.identity.schemas import (
    AuditEventOut,
    InvitationAccept,
    InvitationCreate,
    InvitationCreated,
    InvitationOut,
    InvitationPeek,
    MemberOut,
    MemberUpdate,
    TenantCreate,
    TenantOut,
    TenantUpdate,
)

router = APIRouter(tags=["workspace"])
Paging = Annotated[PageParams, Depends(page_params)]

_INVITATION_STATUS = """
    CASE WHEN revoked_at IS NOT NULL THEN 'revoked'
         WHEN accepted_at IS NOT NULL THEN 'accepted'
         WHEN expires_at <= now() THEN 'expired'
         ELSE 'pending' END
"""


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


@router.post("/tenants", response_model=TenantOut, status_code=201, operation_id="createTenant")
def create_tenant(body: TenantCreate, principal: CurrentPrincipal, db: UserSession) -> TenantOut:
    tenant_id = db.execute(text("SELECT tenant_create(:name)"), {"name": body.name}).scalar_one()
    db.execute(
        text("UPDATE sessions SET active_tenant_id = :t WHERE id = :id"),
        {"t": tenant_id, "id": principal.session_id},
    )
    row = db.execute(text("SELECT id, name, currency, timezone FROM tenants WHERE id = :id"), {"id": tenant_id}).one()
    return TenantOut(id=row.id, name=row.name, currency=row.currency, timezone=row.timezone)


@router.get("/tenant", response_model=TenantOut, operation_id="getTenant")
def get_tenant(ctx: Tenant) -> TenantOut:
    row = ctx.db.execute(
        text("SELECT id, name, currency, timezone FROM tenants WHERE id = :id"),
        {"id": ctx.tenant_id},
    ).one()
    return TenantOut(id=row.id, name=row.name, currency=row.currency, timezone=row.timezone)


@router.patch("/tenant", response_model=TenantOut, operation_id="updateTenant")
def update_tenant(body: TenantUpdate, ctx: TenantContext = tenant_with(Permission.TENANT_SETTINGS)) -> TenantOut:
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if changes:
        assignments = ", ".join(f"{column} = :{column}" for column in changes)  # fixed field names
        ctx.db.execute(
            text(f"UPDATE tenants SET {assignments} WHERE id = :id"),
            {**changes, "id": ctx.tenant_id},
        )
        record_audit(
            ctx.db,
            tenant_id=ctx.tenant_id,
            actor_id=ctx.user_id,
            action="tenant.updated",
            target_type="tenant",
            target_id=str(ctx.tenant_id),
            data={"fields": sorted(changes)},
        )
    return get_tenant(ctx)


# --- members ---------------------------------------------------------------------------------


@router.get("/members", response_model=Page[MemberOut], operation_id="listMembers")
def list_members(paging: Paging, ctx: TenantContext = tenant_with(Permission.MEMBERS_READ)) -> Page[MemberOut]:
    total = ctx.db.execute(
        text("SELECT count(*) FROM memberships WHERE tenant_id = :t"), {"t": ctx.tenant_id}
    ).scalar_one()
    result = ctx.db.execute(
        text(
            """
            SELECT m.user_id, u.email, u.display_name, m.role, m.created_at AS joined_at
            FROM memberships m JOIN users u ON u.id = m.user_id
            WHERE m.tenant_id = :t
            ORDER BY lower(u.email), m.user_id LIMIT :limit OFFSET :offset
            """
        ),
        {"t": ctx.tenant_id, "limit": paging.limit, "offset": paging.offset},
    ).mappings()
    return Page(
        items=[MemberOut(**row) for row in result],
        total=total,
        limit=paging.limit,
        offset=paging.offset,
    )


def _member_role(ctx: TenantContext, user_id: UUID) -> Role:
    role = ctx.db.execute(
        text("SELECT role FROM memberships WHERE tenant_id = :t AND user_id = :u FOR UPDATE"),
        {"t": ctx.tenant_id, "u": user_id},
    ).scalar_one_or_none()
    if role is None:
        raise NotFoundError("Member not found.")
    return Role(role)


def _raise_owner_rule(exc: DBAPIError) -> None:
    if "at least one owner" in str(exc.orig):
        raise ConflictError("A workspace must keep at least one owner.") from exc
    raise exc


@router.patch("/members/{user_id}", response_model=MemberOut, operation_id="updateMember")
def update_member(
    user_id: UUID, body: MemberUpdate, ctx: TenantContext = tenant_with(Permission.MEMBERS_MANAGE)
) -> MemberOut:
    current = _member_role(ctx, user_id)
    if not can_assign_role(ctx.role, current=current, new=body.role):
        raise PermissionDeniedError("Only an owner can change owner roles.")
    try:
        ctx.db.execute(
            text("UPDATE memberships SET role = :r WHERE tenant_id = :t AND user_id = :u"),
            {"r": body.role.value, "t": ctx.tenant_id, "u": user_id},
        )
    except DBAPIError as exc:
        _raise_owner_rule(exc)
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="member.role_changed",
        target_type="user",
        target_id=str(user_id),
        data={"from": current.value, "to": body.role.value},
    )
    row = (
        ctx.db.execute(
            text(
                """
            SELECT m.user_id, u.email, u.display_name, m.role, m.created_at AS joined_at
            FROM memberships m JOIN users u ON u.id = m.user_id
            WHERE m.tenant_id = :t AND m.user_id = :u
            """
            ),
            {"t": ctx.tenant_id, "u": user_id},
        )
        .mappings()
        .one()
    )
    return MemberOut(**row)


@router.delete("/members/{user_id}", status_code=204, operation_id="removeMember")
def remove_member(user_id: UUID, ctx: TenantContext = tenant_with(Permission.MEMBERS_MANAGE)) -> None:
    current = _member_role(ctx, user_id)
    if not can_assign_role(ctx.role, current=current, new=None):
        raise PermissionDeniedError("Only an owner can remove an owner.")
    try:
        ctx.db.execute(
            text("DELETE FROM memberships WHERE tenant_id = :t AND user_id = :u"),
            {"t": ctx.tenant_id, "u": user_id},
        )
    except DBAPIError as exc:
        _raise_owner_rule(exc)
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="member.removed",
        target_type="user",
        target_id=str(user_id),
        data={"role": current.value},
    )


# --- invitations -----------------------------------------------------------------------------


@router.get("/invitations", response_model=Page[InvitationOut], operation_id="listInvitations")
def list_invitations(
    paging: Paging, ctx: TenantContext = tenant_with(Permission.MEMBERS_MANAGE)
) -> Page[InvitationOut]:
    total = ctx.db.execute(
        text("SELECT count(*) FROM invitations WHERE tenant_id = :t"), {"t": ctx.tenant_id}
    ).scalar_one()
    result = ctx.db.execute(
        text(
            f"""
            SELECT id, email, role, {_INVITATION_STATUS} AS status, expires_at, created_at
            FROM invitations WHERE tenant_id = :t
            ORDER BY created_at DESC, id LIMIT :limit OFFSET :offset
            """
        ),
        {"t": ctx.tenant_id, "limit": paging.limit, "offset": paging.offset},
    ).mappings()
    return Page(
        items=[InvitationOut(**row) for row in result],
        total=total,
        limit=paging.limit,
        offset=paging.offset,
    )


@router.post(
    "/invitations",
    response_model=InvitationCreated,
    status_code=201,
    operation_id="createInvitation",
)
def create_invitation(
    body: InvitationCreate, ctx: TenantContext = tenant_with(Permission.MEMBERS_MANAGE)
) -> InvitationCreated:
    if not can_assign_role(ctx.role, current=None, new=body.role):
        raise PermissionDeniedError("Only an owner can invite another owner.")
    email = body.email
    already_member = ctx.db.execute(
        text(
            """
            SELECT 1 FROM memberships m JOIN users u ON u.id = m.user_id
            WHERE m.tenant_id = :t AND u.email = :e
            """
        ),
        {"t": ctx.tenant_id, "e": email},
    ).scalar_one_or_none()
    if already_member:
        raise ConflictError("That person is already a member of this workspace.")
    from app.modules.billing import entitlements

    entitlements.require_room(ctx.db, ctx.tenant_id, "seats", "Inviting another member")
    settings = get_settings()
    token = new_token()
    row = (
        ctx.db.execute(
            text(
                """
            INSERT INTO invitations (tenant_id, email, role, token_hash, invited_by, expires_at)
            VALUES (:t, :e, :r, :h, :by, :exp)
            RETURNING id, email, role, 'pending' AS status, expires_at, created_at
            """
            ),
            {
                "t": ctx.tenant_id,
                "e": email,
                "r": body.role.value,
                "h": hash_token(token),
                "by": ctx.user_id,
                "exp": utcnow() + timedelta(hours=settings.invitation_ttl_hours),
            },
        )
        .mappings()
        .one()
    )
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="invitation.created",
        target_type="invitation",
        target_id=str(row["id"]),
        data={"role": body.role.value},
    )
    return InvitationCreated(
        **row,
        token=token,
        accept_url=f"{settings.public_base_url.rstrip('/')}/invitations/accept?token={token}",
    )


@router.delete("/invitations/{invitation_id}", status_code=204, operation_id="revokeInvitation")
def revoke_invitation(invitation_id: UUID, ctx: TenantContext = tenant_with(Permission.MEMBERS_MANAGE)) -> None:
    revoked = ctx.db.execute(
        text(
            """
            UPDATE invitations SET revoked_at = now()
            WHERE tenant_id = :t AND id = :id AND revoked_at IS NULL AND accepted_at IS NULL
            RETURNING id
            """
        ),
        {"t": ctx.tenant_id, "id": invitation_id},
    ).scalar_one_or_none()
    if revoked is None:
        raise NotFoundError("Invitation not found or no longer pending.")
    record_audit(
        ctx.db,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.user_id,
        action="invitation.revoked",
        target_type="invitation",
        target_id=str(invitation_id),
    )


@router.get("/invitations/lookup", response_model=InvitationPeek, operation_id="peekInvitation")
def peek_invitation(token: Annotated[str, Query(min_length=20, max_length=200)]) -> InvitationPeek:
    """Public: lets an invited person see what they are accepting. Needs the exact token."""
    with session_scope() as db:
        row = db.execute(text("SELECT * FROM invitation_peek(:h)"), {"h": hash_token(token)}).mappings().one_or_none()
    if row is None:
        raise NotFoundError("Invitation not found.")
    return InvitationPeek(**row)


@router.post("/invitations/accept", response_model=TenantOut, operation_id="acceptInvitation")
def accept_invitation(body: InvitationAccept, principal: CurrentPrincipal, db: UserSession) -> TenantOut:
    try:
        tenant_id = db.execute(text("SELECT invitation_accept(:h)"), {"h": hash_token(body.token)}).scalar_one()
    except (DBAPIError, IntegrityError) as exc:
        state = _sqlstate(exc)
        if state == "P0002":
            raise NotFoundError("Invitation not found.") from exc
        if state == "55000":
            raise ConflictError("This invitation has expired or was already used.") from exc
        if state == "42501":
            raise PermissionDeniedError("This invitation was sent to a different email address.") from exc
        raise AppError("The invitation could not be accepted.") from exc
    db.execute(
        text("UPDATE sessions SET active_tenant_id = :t WHERE id = :id"),
        {"t": tenant_id, "id": principal.session_id},
    )
    row = db.execute(text("SELECT id, name, currency, timezone FROM tenants WHERE id = :id"), {"id": tenant_id}).one()
    return TenantOut(id=row.id, name=row.name, currency=row.currency, timezone=row.timezone)


# --- audit -----------------------------------------------------------------------------------


@router.get("/audit-events", response_model=Page[AuditEventOut], operation_id="listAuditEvents")
def list_audit_events(paging: Paging, ctx: TenantContext = tenant_with(Permission.AUDIT_READ)) -> Page[AuditEventOut]:
    total = ctx.db.execute(
        text("SELECT count(*) FROM audit_events WHERE tenant_id = :t"), {"t": ctx.tenant_id}
    ).scalar_one()
    result = ctx.db.execute(
        text(
            """
            SELECT id, occurred_at, actor_type, actor_id, action, target_type, target_id,
                   origin, correlation_id, data
            FROM audit_events WHERE tenant_id = :t
            ORDER BY occurred_at DESC, id LIMIT :limit OFFSET :offset
            """
        ),
        {"t": ctx.tenant_id, "limit": paging.limit, "offset": paging.offset},
    ).mappings()
    return Page(
        items=[AuditEventOut(**row) for row in result],
        total=total,
        limit=paging.limit,
        offset=paging.offset,
    )
