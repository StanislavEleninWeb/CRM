"""Request authentication, tenant context and permission checks.

Authorisation never trusts a tenant identifier supplied by the client. The
active tenant is stored on the server-side session and the member's role is
re-read from the database inside every request transaction.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

import httpx
import redis
from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core import apikeys
from app.core.config import get_settings
from app.core.db import RlsContext, session_scope
from app.core.errors import PermissionDeniedError, UnauthenticatedError
from app.core.security import CSRF_HEADER, SESSION_COOKIE, constant_time_equal, hash_token
from app.modules.identity.oidc import OidcClient
from app.modules.identity.permissions import Permission, Role, permissions_for

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class Principal:
    """An authenticated browser session, or an API key acting as the member who created it."""

    user_id: UUID
    session_id: UUID
    active_tenant_id: UUID | None
    csrf_token: str
    mfa_claimed: bool
    api_key_id: UUID | None = None
    scopes: frozenset[str] | None = None


@dataclass(frozen=True)
class TenantContext:
    """An authenticated member acting inside one tenant, with an open transaction."""

    principal: Principal
    tenant_id: UUID
    role: Role
    permissions: frozenset[Permission]
    db: Session
    support: bool = False
    support_communications: bool = False

    @property
    def user_id(self) -> UUID:
        return self.principal.user_id

    def require_communications(self) -> None:
        """Email conversations and drafts are closed to support access unless the owner included them."""
        if self.support and not self.support_communications:
            raise PermissionDeniedError("This support access does not include communications.")

    def require_person(self, what: str) -> None:
        """For decisions that record a person's judgement or loosen a restriction: never an API key."""
        if self.principal.api_key_id is not None:
            raise PermissionDeniedError(f"{what} needs a signed-in person, not an API key.")

    def require(self, permission: Permission) -> None:
        if permission not in self.permissions:
            raise PermissionDeniedError("You do not have permission to do this.")


@lru_cache
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


@lru_cache
def _http_client() -> httpx.Client:
    return httpx.Client(follow_redirects=False)


def get_http_client() -> httpx.Client:
    return _http_client()


@lru_cache
def _oidc_client() -> OidcClient:
    return OidcClient(get_settings(), _http_client())


def get_oidc_client() -> OidcClient:
    return _oidc_client()


def lookup_session(token: str) -> Principal | None:
    with session_scope() as session:
        row = session.execute(text("SELECT * FROM auth_lookup_session(:h)"), {"h": hash_token(token)}).one_or_none()
    if row is None:
        return None
    return Principal(
        user_id=row.user_id,
        session_id=row.session_id,
        active_tenant_id=row.active_tenant_id,
        csrf_token=row.csrf_token,
        mfa_claimed=row.mfa_claimed,
    )


def _key_principal(request: Request, token: str) -> Principal:
    identity = apikeys.authenticate(token)
    if identity is None:
        raise UnauthenticatedError("The API key is not valid, has expired or was revoked.")
    apikeys.check_rate(get_redis(), identity)
    request.state.api_key = identity
    return Principal(
        user_id=identity.acting_user_id,
        session_id=identity.key_id,
        active_tenant_id=identity.tenant_id,
        csrf_token="",
        mfa_claimed=False,
        api_key_id=identity.key_id,
        scopes=identity.scopes,
    )


def get_principal(request: Request) -> Principal:
    authorization = request.headers.get("Authorization", "")
    if authorization[:7].lower() == "bearer " and apikeys.looks_like_key(authorization[7:].strip()):
        # A key never rides on a browser session: no cookie is read and no CSRF token applies.
        return _key_principal(request, authorization[7:].strip())
    token = request.cookies.get(SESSION_COOKIE)
    principal = lookup_session(token) if token else None
    if principal is None:
        raise UnauthenticatedError("Sign in to continue.")
    if request.method not in SAFE_METHODS:
        supplied = request.headers.get(CSRF_HEADER, "")
        if not supplied or not constant_time_equal(supplied, principal.csrf_token):
            raise PermissionDeniedError("The request could not be verified. Reload and try again.")
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def get_user_session(principal: CurrentPrincipal) -> Iterator[Session]:
    """A transaction that acts as the user, with no tenant selected."""
    if principal.api_key_id is not None:
        raise PermissionDeniedError("This action needs a signed-in person, not an API key.")
    with session_scope(RlsContext(user_id=principal.user_id)) as session:
        yield session


def get_tenant_context(principal: CurrentPrincipal, request: Request) -> Iterator[TenantContext]:
    if principal.active_tenant_id is None:
        raise PermissionDeniedError("Select or create a workspace first.")
    context = RlsContext(user_id=principal.user_id, tenant_id=principal.active_tenant_id)
    with session_scope(context) as session:
        role = session.execute(
            text("SELECT role FROM memberships WHERE tenant_id = :t AND user_id = :u"),
            {"t": principal.active_tenant_id, "u": principal.user_id},
        ).scalar_one_or_none()
        support = None
        if role is None and principal.api_key_id is None:
            # Not a member: the only other way in is support access the owner granted, and only while it lasts.
            support = session.execute(
                text(
                    "SELECT include_communications FROM support_grants WHERE tenant_id = :t AND grantee_user_id = :u "
                    "AND revoked_at IS NULL AND expires_at > now() ORDER BY expires_at DESC LIMIT 1"
                ),
                {"t": principal.active_tenant_id, "u": principal.user_id},
            ).one_or_none()
        if support is not None:
            if request.method not in SAFE_METHODS:
                raise PermissionDeniedError("Support access is read-only.")
            yield TenantContext(
                principal=principal,
                tenant_id=principal.active_tenant_id,
                role=Role.READ_ONLY,
                permissions=frozenset({Permission.CRM_READ, Permission.REPORTS_READ}),
                db=session,
                support=True,
                support_communications=bool(support.include_communications),
            )
            return
        if role is None:
            raise PermissionDeniedError("You are no longer a member of this workspace.")
        resolved = Role(role)
        if principal.api_key_id is not None:
            # Carried on the session so every audit entry written in this request names the key.
            session.info["api_key_id"] = str(principal.api_key_id)
        if request.method not in SAFE_METHODS and "/billing" not in request.url.path:
            # A restricted workspace can read, export and reach billing. Enforced here, so it holds for
            # people and API keys alike, whatever the page shows.
            from app.modules.billing import entitlements

            entitlement = entitlements.evaluate(session, principal.active_tenant_id)
            if entitlement.restricted:
                raise entitlements.PaymentRequiredError(entitlement.reason or "The subscription is not active.")
        granted = permissions_for(resolved)
        if principal.scopes is not None:
            # A key holds its scopes, capped by what keys may ever do and by its creator's role today.
            granted = frozenset(p for p in granted if p in apikeys.ALLOWED_SCOPES and p.value in principal.scopes)
        yield TenantContext(
            principal=principal,
            tenant_id=principal.active_tenant_id,
            role=resolved,
            permissions=granted,
            db=session,
        )


# ``scope="function"`` makes the transaction commit before the response is sent, so a
# failed commit becomes an error response instead of a success that never happened.
UserSession = Annotated[Session, Depends(get_user_session, scope="function")]
Tenant = Annotated[TenantContext, Depends(get_tenant_context, scope="function")]


def require(permission: Permission) -> Callable[[TenantContext], TenantContext]:
    def dependency(context: Tenant) -> TenantContext:
        context.require(permission)
        return context

    return dependency


def tenant_with(permission: Permission) -> Any:
    return Depends(require(permission), scope="function")
