"""Sign-in, sessions and the current user."""

import json
from datetime import timedelta
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

import redis
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.deps import CurrentPrincipal, UserSession, get_oidc_client, get_redis
from app.core.errors import NotFoundError, PermissionDeniedError, UnauthenticatedError
from app.core.logging import get_logger
from app.core.pagination import Page
from app.core.security import (
    CSRF_COOKIE,
    LOGIN_COOKIE,
    SESSION_COOKIE,
    clear_cookie,
    constant_time_equal,
    hash_token,
    new_token,
    pkce_challenge,
    set_cookie,
)
from app.core.time import utcnow
from app.modules.identity.oidc import OidcClient
from app.modules.identity.permissions import Role, permissions_for
from app.modules.identity.schemas import (
    ActiveTenant,
    Me,
    SessionOut,
    SwitchTenant,
    TenantSummary,
    UserOut,
)

log = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])
LOGIN_TTL_SECONDS = 600


def _login_key(state: str) -> str:
    return f"crm:oidc:login:{state}"


def _safe_return_path(value: str | None) -> str:
    """Only same-site relative paths are accepted as a post-login destination."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    parts = urlsplit(value)
    return value if not parts.scheme and not parts.netloc else "/"


@router.get("/login", operation_id="login", status_code=307, response_class=RedirectResponse)
def login(
    oidc: Annotated[OidcClient, Depends(get_oidc_client)],
    store: Annotated[redis.Redis, Depends(get_redis)],
    return_to: Annotated[str | None, Query(max_length=500)] = None,
) -> RedirectResponse:
    state, nonce, verifier = new_token(), new_token(), new_token(48)
    store.set(
        _login_key(state),
        json.dumps(
            {"nonce": nonce, "verifier": verifier, "return_to": _safe_return_path(return_to)}
        ),
        ex=LOGIN_TTL_SECONDS,
    )
    url = oidc.authorization_url(state=state, nonce=nonce, code_challenge=pkce_challenge(verifier))
    response = RedirectResponse(url, status_code=307)
    # Binds the login attempt to this browser, so a callback cannot be planted in another one.
    set_cookie(response, LOGIN_COOKIE, state, max_age=LOGIN_TTL_SECONDS)
    return response


@router.get(
    "/callback", operation_id="loginCallback", status_code=303, response_class=RedirectResponse
)
def callback(
    request: Request,
    oidc: Annotated[OidcClient, Depends(get_oidc_client)],
    store: Annotated[redis.Redis, Depends(get_redis)],
    code: Annotated[str | None, Query(max_length=2000)] = None,
    state: Annotated[str | None, Query(max_length=200)] = None,
    error: Annotated[str | None, Query(max_length=200)] = None,
) -> RedirectResponse:
    settings = get_settings()
    bound_state = request.cookies.get(LOGIN_COOKIE, "")
    if error or not code or not state or not bound_state:
        raise UnauthenticatedError("Sign-in was cancelled or could not be completed.")
    if not constant_time_equal(state, bound_state):
        raise UnauthenticatedError("Sign-in could not be verified. Start again.")
    pending_raw = store.getdel(_login_key(state))  # single use
    if not isinstance(pending_raw, str):
        raise UnauthenticatedError("Sign-in expired. Start again.")
    pending = json.loads(pending_raw)

    identity = oidc.redeem(code=code, code_verifier=pending["verifier"], nonce=pending["nonce"])

    session_token, csrf_token = new_token(), new_token()
    ttl = timedelta(hours=settings.session_ttl_hours)
    with session_scope() as db:
        user_id = db.execute(
            text("SELECT auth_upsert_user(:iss, :sub, :email, :verified, :name)"),
            {
                "iss": identity.issuer,
                "sub": identity.subject,
                "email": identity.email,
                "verified": identity.email_verified,
                "name": identity.name,
            },
        ).scalar_one()
        db.execute(
            text("SELECT auth_create_session(:user_id, :hash, :csrf, :expires, :ua, :mfa)"),
            {
                "user_id": user_id,
                "hash": hash_token(session_token),
                "csrf": csrf_token,
                "expires": utcnow() + ttl,
                "ua": request.headers.get("user-agent", ""),
                "mfa": identity.mfa,
            },
        )
    log.info("login_succeeded", user_id=str(user_id))

    target = settings.public_base_url.rstrip("/") + pending["return_to"]
    response = RedirectResponse(target, status_code=303)
    max_age = int(ttl.total_seconds())
    set_cookie(response, SESSION_COOKIE, session_token, max_age=max_age)
    set_cookie(response, CSRF_COOKIE, csrf_token, max_age=max_age, http_only=False)
    clear_cookie(response, LOGIN_COOKIE)
    return response


@router.post("/logout", operation_id="logout", status_code=204)
def logout(principal: CurrentPrincipal, db: UserSession, response: Response) -> None:
    db.execute(
        text("UPDATE sessions SET revoked_at = now() WHERE id = :id"), {"id": principal.session_id}
    )
    clear_cookie(response, SESSION_COOKIE)
    clear_cookie(response, CSRF_COOKIE)


@router.get("/me", response_model=Me, operation_id="getMe")
def me(principal: CurrentPrincipal, db: UserSession) -> Me:
    user = db.execute(
        text("SELECT id, email, display_name FROM users WHERE id = :id"), {"id": principal.user_id}
    ).one()
    tenants = db.execute(
        text(
            """
            SELECT t.id, t.name, t.currency, t.timezone, m.role
            FROM memberships m JOIN tenants t ON t.id = m.tenant_id
            WHERE m.user_id = :u ORDER BY lower(t.name), t.id
            """
        ),
        {"u": principal.user_id},
    ).all()
    active = next((t for t in tenants if t.id == principal.active_tenant_id), None)
    return Me(
        user=UserOut(id=user.id, email=user.email, display_name=user.display_name),
        active_tenant=(
            ActiveTenant(
                id=active.id,
                name=active.name,
                role=Role(active.role),
                permissions=sorted(permissions_for(Role(active.role))),
                currency=active.currency,
                timezone=active.timezone,
            )
            if active
            else None
        ),
        tenants=[TenantSummary(id=t.id, name=t.name, role=Role(t.role)) for t in tenants],
        csrf_token=principal.csrf_token,
        mfa_claimed=principal.mfa_claimed,
    )


@router.post("/switch-tenant", status_code=204, operation_id="switchTenant")
def switch_tenant(body: SwitchTenant, principal: CurrentPrincipal, db: UserSession) -> None:
    is_member = db.execute(
        text("SELECT 1 FROM memberships WHERE tenant_id = :t AND user_id = :u"),
        {"t": body.tenant_id, "u": principal.user_id},
    ).scalar_one_or_none()
    if not is_member:
        # Same answer whether the workspace does not exist or the user is not a member.
        raise PermissionDeniedError("You are not a member of that workspace.")
    db.execute(
        text("UPDATE sessions SET active_tenant_id = :t WHERE id = :id"),
        {"t": body.tenant_id, "id": principal.session_id},
    )


@router.get("/sessions", response_model=Page[SessionOut], operation_id="listSessions")
def list_sessions(principal: CurrentPrincipal, db: UserSession) -> Page[SessionOut]:
    result = db.execute(
        text(
            """
            SELECT id, created_at, last_seen_at, expires_at, user_agent FROM sessions
            WHERE user_id = :u AND revoked_at IS NULL AND expires_at > now()
            ORDER BY last_seen_at DESC LIMIT 200
            """
        ),
        {"u": principal.user_id},
    ).all()
    items = [
        SessionOut(
            id=r.id,
            created_at=r.created_at,
            last_seen_at=r.last_seen_at,
            expires_at=r.expires_at,
            user_agent=r.user_agent,
            current=r.id == principal.session_id,
        )
        for r in result
    ]
    return Page(items=items, total=len(items), limit=200, offset=0)


@router.delete("/sessions/{session_id}", status_code=204, operation_id="revokeSession")
def revoke_session(session_id: UUID, db: UserSession) -> None:
    updated = db.execute(
        text(
            "UPDATE sessions SET revoked_at = now() "
            "WHERE id = :id AND revoked_at IS NULL RETURNING id"
        ),
        {"id": session_id},
    ).scalar_one_or_none()
    if updated is None:
        raise NotFoundError("Session not found.")


@router.post("/sessions/revoke-others", status_code=204, operation_id="revokeOtherSessions")
def revoke_other_sessions(principal: CurrentPrincipal, db: UserSession) -> None:
    db.execute(
        text("UPDATE sessions SET revoked_at = now() WHERE id <> :id AND revoked_at IS NULL"),
        {"id": principal.session_id},
    )
