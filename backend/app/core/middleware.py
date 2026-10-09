"""Request and correlation IDs."""

import re
import time
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import get_logger

log = get_logger("http")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
REQUEST_ID_HEADER = b"x-request-id"
CORRELATION_ID_HEADER = b"x-correlation-id"


class CorrelationMiddleware:
    """Assign a request ID, accept a well-formed correlation ID, and echo both."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        request_id = uuid.uuid4().hex
        supplied = headers.get(CORRELATION_ID_HEADER, b"").decode("latin-1")
        correlation_id = supplied if _SAFE_ID.match(supplied) else request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id, correlation_id=correlation_id)
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                message["headers"].append((REQUEST_ID_HEADER, request_id.encode()))
                message["headers"].append((CORRELATION_ID_HEADER, correlation_id.encode()))
                if scope["path"].startswith("/api/") and not any(
                    k.lower() == b"cache-control" for k, _ in message["headers"]
                ):
                    # Answers carry a workspace's data: neither the browser nor a proxy may keep them.
                    message["headers"].append((b"cache-control", b"no-store"))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if scope["path"] not in ("/healthz", "/readyz"):
                log.info(
                    "request",
                    method=scope["method"],
                    path=scope["path"],
                    status=status,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )
            structlog.contextvars.clear_contextvars()
