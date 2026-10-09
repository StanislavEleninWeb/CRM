"""Re-encrypt every stored secret (provider credentials, mailbox tokens, webhook secrets) with the current key version.

Key rotation procedure:
  1. Generate a new key and put it FIRST in SECRET_ENCRYPTION_KEYS, keeping the old one after it.
  2. Restart the services, then run:  python -m app.modules.providers.rotate
  3. When it reports nothing left on old versions, remove the old key and restart.

Tenants are listed with the migration role; each credential is re-sealed inside its own
tenant context, so the associated data that binds a secret to its tenant is unchanged.
"""

import sys
from uuid import UUID

from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.core.db import RlsContext, session_scope
from app.core.secrets import Sealed, SecretError, get_keyring

# Every place a secret is stored: table, the columns holding it, and the "provider" it is bound to.
SOURCES = (
    ("provider_connections", "secret_ciphertext", "secret_nonce", "secret_key_version", "provider"),
    ("mailboxes", "token_ciphertext", "token_nonce", "token_key_version", "provider"),
    ("webhook_endpoints", "secret_ciphertext", "secret_nonce", "secret_key_version", "'webhook'"),
    (
        "webhook_endpoints",
        "previous_secret_ciphertext",
        "previous_secret_nonce",
        "previous_secret_key_version",
        "'webhook'",
    ),
)


def reseal_all() -> dict[str, int]:
    settings = get_settings()
    if not settings.migration_database_url:
        raise SystemExit("MIGRATION_DATABASE_URL is required to list tenants")
    ring = get_keyring()
    current = ring.current_version
    engine = create_engine(settings.migration_database_url)
    with engine.connect() as conn:
        tenants = [row[0] for row in conn.execute(text("SELECT id FROM tenants ORDER BY created_at"))]
    engine.dispose()
    counts = {"resealed": 0, "already_current": 0, "unreadable": 0}
    for tenant_id in tenants:
        for table, cipher, nonce, version, provider in SOURCES:
            with session_scope(RlsContext(tenant_id=UUID(str(tenant_id)))) as db:
                rows = db.execute(
                    text(
                        f"SELECT id, {provider} AS provider, {cipher} AS ciphertext, {nonce} AS nonce, {version} AS key_version "
                        f"FROM {table} WHERE tenant_id = :t AND {cipher} IS NOT NULL FOR UPDATE"
                    ),
                    {"t": tenant_id},
                ).all()
                for row in rows:
                    if row.key_version == current:
                        counts["already_current"] += 1
                        continue
                    scope = {"tenant_id": tenant_id, "provider": row.provider, "connection_id": row.id}
                    try:
                        plain = ring.open(Sealed(bytes(row.ciphertext), bytes(row.nonce), row.key_version), **scope)
                    except SecretError:
                        counts["unreadable"] += 1  # the old key is missing; it has to be entered or connected again
                        continue
                    sealed = ring.seal(plain, **scope)
                    db.execute(
                        text(
                            f"UPDATE {table} SET {cipher} = :c, {nonce} = :n, {version} = :v WHERE tenant_id = :t AND id = :id"
                        ),
                        {
                            "c": sealed.ciphertext,
                            "n": sealed.nonce,
                            "v": sealed.key_version,
                            "t": tenant_id,
                            "id": row.id,
                        },
                    )
                    counts["resealed"] += 1
    return counts


if __name__ == "__main__":
    result = reseal_all()
    print(
        f"Re-sealed {result['resealed']}; already current {result['already_current']}; unreadable {result['unreadable']}"
    )
    sys.exit(1 if result["unreadable"] else 0)
