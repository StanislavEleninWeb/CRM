"""Field-level source policy: what may be stored, exported or used for scoring, per source."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class FieldPolicy:
    may_store: bool
    may_export: bool
    may_use_for_scoring: bool
    retention_days: int | None
    status: str


DENY = FieldPolicy(False, False, False, None, "unverified")


class SourcePolicy:
    """A tenant's policies, looked up as ``(source_type, field)`` with ``*`` as the fallback."""

    def __init__(self, rows: list[Any]) -> None:
        self._rules = {
            (r["source_type"], r["field"]): FieldPolicy(
                r["may_store"], r["may_export"], r["may_use_for_scoring"], r["retention_days"], r["status"]
            )
            for r in rows
        }

    @classmethod
    def load(cls, db: Session, tenant_id: UUID) -> "SourcePolicy":
        rows = db.execute(text("SELECT * FROM source_policies WHERE tenant_id = :t"), {"t": tenant_id}).mappings().all()
        return cls(list(rows))

    def for_field(self, source_type: str, field: str) -> FieldPolicy:
        # An unknown source is denied: rights that were never reviewed are not assumed.
        return self._rules.get((source_type, field)) or self._rules.get((source_type, "*")) or DENY

    def storable(self, source_type: str, values: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Split provider values into what may be kept and the names of fields that were dropped."""
        kept: dict[str, Any] = {}
        dropped: list[str] = []
        for field, value in values.items():
            if value is None:
                continue
            if self.for_field(source_type, field).may_store:
                kept[field] = value
            else:
                dropped.append(field)
        return kept, sorted(dropped)

    def may_export(self, source_type: str, field: str = "*") -> bool:
        return self.for_field(source_type, field).may_export

    def may_score(self, source_type: str, field: str = "*") -> bool:
        return self.for_field(source_type, field).may_use_for_scoring
