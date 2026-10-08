"""Structured JSON logging with redaction of secrets."""

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"(pass(word)?|secret|token|authorization|cookie|api[_-]?key|credential|session|dsn|database_url)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
# Secrets passed as URL query parameters, for example ``?key=...`` or ``&access_token=...``.
_QUERY_SECRET = re.compile(
    r"(?i)(?P<name>[?&](?:api[_-]?key|key|token|access_token|refresh_token|id_token|secret|client_secret|password|signature|sig|code)=)[^&\s#\"']+"
)


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        value = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", value)
        value = _QUERY_SECRET.sub(lambda m: f"{m.group('name')}{REDACTED}", value)
        return _URL_CREDENTIALS.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", value)
    if isinstance(value, dict):
        return {k: (REDACTED if _SENSITIVE_KEY.search(str(k)) else redact_value(v)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_value(v) for v in value]
    return value


def redact_processor(_logger: Any, _method: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if _SENSITIVE_KEY.search(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = redact_value(event_dict[key])
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(stream=sys.stdout, level=level.upper(), format="%(message)s", force=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redact_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
