"""Signed opt-out links. A link identifies one tenant and one address and cannot be forged or altered."""

import base64
import hashlib
import hmac
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import RlsContext, session_scope
from app.core.normalize import normalize_email

router = APIRouter()


def _signature(payload: bytes) -> bytes:
    return hmac.new(get_settings().session_secret.encode() + b":unsubscribe", payload, hashlib.sha256).digest()[:16]


def make_token(tenant_id: UUID, address: str) -> str:
    payload = tenant_id.bytes + address.lower().encode()
    return base64.urlsafe_b64encode(_signature(payload) + payload).decode().rstrip("=")


def read_token(token: str) -> tuple[UUID, str] | None:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, TypeError):
        return None
    if len(raw) < 16 + 16 + 3:
        return None
    signature, payload = raw[:16], raw[16:]
    if not hmac.compare_digest(signature, _signature(payload)):
        return None
    address = normalize_email(payload[16:].decode("utf-8", errors="ignore"))
    return (UUID(bytes=payload[:16]), address) if address else None


def _apply(token: str) -> bool:
    from app.modules.email.router import suppress

    parsed = read_token(token)
    if parsed is None:
        return False
    tenant_id, address = parsed
    with session_scope(RlsContext(tenant_id=tenant_id)) as db:
        if db.execute(text("SELECT 1 FROM tenants WHERE id = :t"), {"t": tenant_id}).first() is None:
            return False
        suppress(
            db,
            tenant_id,
            scope="address",
            value=address,
            reason="opt_out",
            source="unsubscribe_link",
            note=None,
            created_by=None,
        )
    return True


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>{title}</title></head><body style="font-family: system-ui, sans-serif; max-width: 32rem; margin: 3rem auto; padding: 0 1rem">
<h1>{title}</h1><p>{body}</p></body></html>"""
Token = Annotated[str, Path(min_length=40, max_length=600)]


@router.post("/unsubscribe/{token}", include_in_schema=False)
def one_click(token: Token) -> HTMLResponse:
    """RFC 8058 one-click: the mail client posts here with no further interaction."""
    done = _apply(token)
    return HTMLResponse(
        PAGE.format(title="Unsubscribed" if done else "Link not valid", body=""), status_code=200 if done else 404
    )


@router.get("/unsubscribe/{token}", include_in_schema=False)
def confirm_page(token: Token) -> HTMLResponse:
    """A link opened by a person (or pre-fetched by a scanner) only shows a button; it changes nothing by itself."""
    if read_token(token) is None:
        return HTMLResponse(
            PAGE.format(title="Link not valid", body="This opt-out link is incomplete or has been altered."),
            status_code=404,
        )
    form = '<form method="post"><button type="submit" style="font: inherit; padding: 0.75rem 1.25rem">Stop emails to this address</button></form>'
    return HTMLResponse(PAGE.format(title="Stop receiving these emails?", body=form))
