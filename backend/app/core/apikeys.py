"""API keys: generation, lookup and rate limiting.

A key acts inside one tenant as the member who created it, limited to the scopes it was
given and never beyond what that member may do now. Only a hash of the key is stored.
"""

import time
from dataclasses import dataclass
from uuid import UUID

import redis
from sqlalchemy import text

from app.core.db import session_scope
from app.core.errors import RateLimitedError
from app.core.security import hash_token, new_token
from app.modules.identity.permissions import Permission

KEY_PREFIX = "crm_"
# What a key can ever hold. Ownership, membership, settings, billing, stored credentials,
# audit history, approvals, deletion and bulk export are for signed-in people only. So is
# anything that records a person's judgement: verifying research, classifying a recipient,
# promoting a candidate (research.review) and saying what happened on a call (calls.log).
ALLOWED_SCOPES: frozenset[Permission] = frozenset(
    {
        Permission.CRM_READ,
        Permission.CRM_WRITE,
        Permission.RESEARCH_RUN,
        Permission.OUTREACH_DRAFT,
        Permission.OUTREACH_SEND,
        Permission.REPORTS_READ,
    }
)


@dataclass(frozen=True)
class KeyIdentity:
    key_id: UUID
    tenant_id: UUID
    acting_user_id: UUID
    scopes: frozenset[str]
    rate_limit_per_minute: int


def generate() -> tuple[str, str]:
    """A new key and the short prefix that identifies it in lists. The key itself is shown once."""
    secret = new_token(32)
    token = f"{KEY_PREFIX}{secret}"
    return token, token[: len(KEY_PREFIX) + 6]


def looks_like_key(value: str) -> bool:
    return value.startswith(KEY_PREFIX)


def authenticate(token: str) -> KeyIdentity | None:
    if not looks_like_key(token) or len(token) > 200:
        return None
    with session_scope() as db:
        row = db.execute(text("SELECT * FROM api_key_lookup(:h)"), {"h": hash_token(token)}).one_or_none()
    if row is None:
        return None
    return KeyIdentity(row.key_id, row.tenant_id, row.acting_user_id, frozenset(row.scopes), row.rate_limit_per_minute)


def check_rate(client: redis.Redis, identity: KeyIdentity) -> None:
    """A fixed one-minute window per key."""
    window = int(time.time() // 60)
    bucket = f"crm:ratelimit:key:{identity.key_id}:{window}"
    used = client.incr(bucket)
    if used == 1:
        client.expire(bucket, 120)
    if int(used) > identity.rate_limit_per_minute:  # type: ignore[arg-type]
        retry_after = 60 - int(time.time() % 60)
        raise RateLimitedError(
            "This key has made too many requests. Try again shortly.", headers={"Retry-After": str(retry_after)}
        )
