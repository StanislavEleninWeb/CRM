"""Attach a billing-provider price to one of this application's plans.

    python -m app.modules.billing.configure test_starter price_123

Run by an operator with the migration role. Plans and their limits are product decisions;
this only records which provider price a plan is sold as. A plan without a price cannot be bought.
"""

import sys

from sqlalchemy import create_engine, text

from app.core.config import get_settings


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].startswith("price_"):
        print("usage: python -m app.modules.billing.configure <plan_code> <price_id>")
        return 2
    url = get_settings().migration_database_url
    if not url:
        print("MIGRATION_DATABASE_URL is required")
        return 2
    engine = create_engine(url)
    with engine.begin() as conn:
        updated = conn.execute(
            text("UPDATE plans SET provider_price_id = :p WHERE code = :c AND is_public RETURNING code, is_test"),
            {"p": argv[1], "c": argv[0]},
        ).one_or_none()
    engine.dispose()
    if updated is None:
        print(f"No purchasable plan is called {argv[0]}.")
        return 1
    print(
        f"{updated.code} is now sold as {argv[1]}"
        + (" (a TEST plan: not an approved price)" if updated.is_test else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
