"""Idempotency for requests made with an API key.

A client that sends ``Idempotency-Key`` on a state-changing request gets the same answer
when it repeats that exact request, and an error when it reuses the key for a different
one. The key is scoped to the tenant and API key.

What this does not promise: if the server stops, or answers with a server error, between
doing the work and recording the answer, the record stays "in progress" and later attempts
are refused rather than run again. The client then has to read the current state. A
request with a key is never executed twice. Answers that say "not now" (rate limited, key
not accepted, subscription or plan limit) are not recorded, so the same key can be used when
the cause has passed.
"""

import hashlib
import json
from typing import Any

from sqlalchemy import text
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import apikeys
from app.core.db import RlsContext, session_scope
from app.core.logging import get_logger

log = get_logger(__name__)
HEADER = b"idempotency-key"
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MAX_BODY = 1_000_000
MAX_STORED_RESPONSE = 1_000_000


def _error(status: int, code: str, message: str) -> tuple[int, bytes]:
    return status, json.dumps({"error": {"code": code, "message": message}}).encode()


class IdempotencyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in MUTATING:
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers") or []}
        key = headers.get(HEADER, b"").decode("latin-1").strip()
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        token = authorization[7:].strip() if authorization[:7].lower() == "bearer " else ""
        if not key or not apikeys.looks_like_key(token):
            await self.app(scope, receive, send)
            return
        if len(key) > 200:
            await self._reply(send, *_error(422, "validation_error", "The idempotency key is too long."))
            return
        identity = apikeys.authenticate(token)
        if identity is None:
            await self.app(scope, receive, send)  # the endpoint answers 401 in the usual shape
            return

        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if len(body) > MAX_BODY:
                await self._reply(
                    send, *_error(413, "payload_too_large", "The request is too large to be made idempotent.")
                )
                return
            if not message.get("more_body"):
                break
        query = scope.get("query_string", b"").decode("latin-1")
        digest = hashlib.sha256(
            b"\n".join([scope["method"].encode(), scope["path"].encode(), query.encode(), body])
        ).hexdigest()
        context = RlsContext(tenant_id=identity.tenant_id)
        params = {"t": identity.tenant_id, "k": identity.key_id, "key": key}

        with session_scope(context) as db:
            created = db.execute(
                text(
                    "INSERT INTO idempotency_keys (tenant_id, api_key_id, key, request_hash) VALUES (:t, :k, :key, :h) "
                    "ON CONFLICT (tenant_id, api_key_id, key) DO NOTHING RETURNING id"
                ),
                {**params, "h": digest},
            ).scalar()
            existing = None
            if created is None:
                existing = db.execute(
                    text(
                        "SELECT request_hash, state, status_code, response_body FROM idempotency_keys "
                        "WHERE tenant_id = :t AND api_key_id = :k AND key = :key"
                    ),
                    params,
                ).one()
        if existing is not None:
            if existing.request_hash != digest:
                await self._reply(
                    send,
                    *_error(
                        422, "idempotency_conflict", "This idempotency key was already used for a different request."
                    ),
                )
            elif existing.state == "completed":
                await self._reply(send, existing.status_code, (existing.response_body or "").encode(), replayed=True)
            else:
                await self._reply(
                    send,
                    *_error(
                        409,
                        "idempotency_in_progress",
                        "A request with this key is still running or its outcome was not recorded. Check the current state before trying a new key.",
                    ),
                )
            return

        sent = False

        async def replay_body() -> Message:
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        captured: dict[str, Any] = {"status": 500, "body": b""}

        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
            elif message["type"] == "http.response.body":
                captured["body"] += message.get("body", b"")
            await send(message)

        try:
            await self.app(scope, replay_body, capture)
        finally:
            with session_scope(context) as db:
                if captured["status"] in (401, 402, 429) or b'"plan_limit_reached"' in captured["body"][:300]:
                    # Nothing was attempted: the same key may be used once the cause has passed.
                    db.execute(
                        text("DELETE FROM idempotency_keys WHERE tenant_id = :t AND api_key_id = :k AND key = :key"),
                        params,
                    )
                elif captured["status"] < 500 and len(captured["body"]) <= MAX_STORED_RESPONSE:
                    db.execute(
                        text(
                            "UPDATE idempotency_keys SET state = 'completed', status_code = :s, response_body = :b, completed_at = now() "
                            "WHERE tenant_id = :t AND api_key_id = :k AND key = :key"
                        ),
                        {**params, "s": captured["status"], "b": captured["body"].decode("utf-8", errors="replace")},
                    )
                # A server error may or may not have done the work. The record stays "in progress",
                # so a retry is refused instead of risking a second execution.

    @staticmethod
    async def _reply(send: Send, status: int, body: bytes, *, replayed: bool = False) -> None:
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        if replayed:
            headers.append((b"idempotency-replayed", b"true"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
