"""Budgets and reservations.

Before any paid work is dispatched, its maximum cost is reserved. A reservation moves
``reserved -> settled | released | unknown``. An operation that timed out is *unknown*:
it may have been charged, so its reservation keeps holding the budget until a person
resolves it. Nothing releases a reservation automatically on a timeout.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from app.core.errors import AppError, ConflictError
from app.core.time import today_in

ZERO = Decimal("0")


class BudgetExceeded(AppError):
    status_code = 409
    code = "budget_exceeded"


class NoBudget(AppError):
    status_code = 409
    code = "no_budget"


@dataclass(frozen=True)
class Reservation:
    id: UUID
    budget_id: UUID
    state: str
    reserved_amount: Decimal
    actual_amount: Decimal | None
    currency: str
    created: bool  # False when an earlier reservation with the same key was returned

    @classmethod
    def from_row(cls, row: RowMapping, *, created: bool) -> "Reservation":
        return cls(
            id=row["id"],
            budget_id=row["budget_id"],
            state=row["state"],
            reserved_amount=row["reserved_amount"],
            actual_amount=row["actual_amount"],
            currency=row["currency"],
            created=created,
        )


def period_start(period: str, today: date) -> date:
    return today.replace(day=1) if period == "month" else date(1970, 1, 1)


def _current_budget(db: Session, tenant_id: UUID, scope: str, *, lock: bool) -> RowMapping | None:
    """The most specific budget for this scope in the current period."""
    timezone = db.execute(text("SELECT timezone FROM tenants WHERE id = :t"), {"t": tenant_id}).scalar_one()
    today = today_in(timezone)
    return (
        db.execute(
            text(
                "SELECT * FROM budgets WHERE tenant_id = :t AND scope IN (:scope, 'all') "
                "AND ((period = 'month' AND period_start = :month) OR period = 'total') "
                "ORDER BY (scope = :scope) DESC, (period = 'month') DESC LIMIT 1" + (" FOR UPDATE" if lock else "")
            ),
            {"t": tenant_id, "scope": scope, "month": period_start("month", today)},
        )
        .mappings()
        .one_or_none()
    )


def reserve(
    db: Session,
    tenant_id: UUID,
    *,
    scope: str,
    amount: Decimal,
    idempotency_key: str,
    purpose: str,
    run_ref: str | None = None,
    connection_id: UUID | None = None,
    created_by: UUID | None = None,
) -> Reservation:
    """Hold ``amount`` against the budget, or raise. Safe to call again with the same key."""
    if amount < ZERO:
        raise ValueError("a reservation cannot be negative")
    existing = _by_key(db, tenant_id, idempotency_key)
    if existing is not None:
        return Reservation.from_row(existing, created=False)
    # Locking the budget row serialises every reservation against it.
    budget = _current_budget(db, tenant_id, scope, lock=True)
    if budget is None:
        raise NoBudget("Set a budget before running paid work.")
    existing = _by_key(db, tenant_id, idempotency_key)  # another transaction may have won while we waited
    if existing is not None:
        return Reservation.from_row(existing, created=False)
    if run_ref is not None:
        active_runs = db.execute(
            text(
                "SELECT count(DISTINCT run_ref) FROM budget_reservations WHERE tenant_id = :t AND budget_id = :b "
                "AND state IN ('reserved', 'unknown') AND run_ref IS NOT NULL AND run_ref <> :r"
            ),
            {"t": tenant_id, "b": budget["id"], "r": run_ref},
        ).scalar_one()
        if active_runs >= budget["max_concurrent_runs"]:
            raise BudgetExceeded(f"Only {budget['max_concurrent_runs']} run(s) may be in progress at once.")
    held = db.execute(
        text(
            "UPDATE budgets SET reserved_amount = reserved_amount + :a WHERE tenant_id = :t AND id = :b "
            "AND NOT overrun AND (allow_overage OR limit_amount - spent_amount - reserved_amount >= :a) RETURNING id"
        ),
        {"a": amount, "t": tenant_id, "b": budget["id"]},
    ).scalar_one_or_none()
    if held is None:
        remaining = max(budget["limit_amount"] - budget["spent_amount"] - budget["reserved_amount"], ZERO)
        raise BudgetExceeded(
            f"This needs up to {amount} {budget['currency']} but only {remaining} {budget['currency']} of the budget is free."
        )
    row = (
        db.execute(
            text(
                "INSERT INTO budget_reservations (tenant_id, budget_id, connection_id, idempotency_key, purpose, run_ref, "
                "reserved_amount, currency, created_by) VALUES (:t, :b, :c, :k, :p, :r, :a, :cur, :u) RETURNING *"
            ),
            {
                "t": tenant_id,
                "b": budget["id"],
                "c": connection_id,
                "k": idempotency_key,
                "p": purpose,
                "r": run_ref,
                "a": amount,
                "cur": budget["currency"],
                "u": created_by,
            },
        )
        .mappings()
        .one()
    )
    return Reservation.from_row(row, created=True)


def _by_key(db: Session, tenant_id: UUID, key: str) -> RowMapping | None:
    return (
        db.execute(
            text("SELECT * FROM budget_reservations WHERE tenant_id = :t AND idempotency_key = :k"),
            {"t": tenant_id, "k": key},
        )
        .mappings()
        .one_or_none()
    )


def _locked(db: Session, tenant_id: UUID, reservation_id: UUID) -> RowMapping:
    row = (
        db.execute(
            text("SELECT * FROM budget_reservations WHERE tenant_id = :t AND id = :r FOR UPDATE"),
            {"t": tenant_id, "r": reservation_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ConflictError("Reservation not found.")
    return row


def settle(
    db: Session,
    tenant_id: UUID,
    reservation_id: UUID,
    *,
    actual: Decimal,
    cost_basis: str,
    provider: str,
    units: dict[str, Any] | None = None,
    billed_to: str = "platform",
    pricing_version: str | None = None,
    note: str | None = None,
) -> Reservation:
    """Record what the work actually cost. Settling twice is a no-op."""
    import json

    row = _locked(db, tenant_id, reservation_id)
    if row["state"] == "settled":
        return Reservation.from_row(row, created=False)
    if row["state"] == "released":
        raise ConflictError("A released reservation cannot be settled.")
    # The real charge is recorded even when it is above the reservation; the budget is then marked
    # overrun, which blocks further reservations until someone raises the limit.
    db.execute(
        text(
            "UPDATE budgets SET reserved_amount = reserved_amount - :held, spent_amount = spent_amount + :actual, "
            "overrun = overrun OR (NOT allow_overage AND spent_amount + :actual + reserved_amount - :held > limit_amount) "
            "WHERE tenant_id = :t AND id = :b"
        ),
        {"held": row["reserved_amount"], "actual": actual, "t": tenant_id, "b": row["budget_id"]},
    )
    updated = (
        db.execute(
            text(
                "UPDATE budget_reservations SET state = 'settled', actual_amount = :a, settled_at = now(), note = COALESCE(:n, note) "
                "WHERE tenant_id = :t AND id = :r RETURNING *"
            ),
            {"a": actual, "n": note, "t": tenant_id, "r": reservation_id},
        )
        .mappings()
        .one()
    )
    db.execute(
        text(
            "INSERT INTO usage_ledger (tenant_id, connection_id, reservation_id, provider, purpose, run_ref, units, amount, "
            "currency, cost_basis, pricing_version, billed_to, note) VALUES (:t, :c, :r, :provider, :purpose, :run, "
            "CAST(:units AS jsonb), :a, :cur, :basis, :pv, :billed, :n)"
        ),
        {
            "t": tenant_id,
            "c": row["connection_id"],
            "r": reservation_id,
            "provider": provider,
            "purpose": row["purpose"],
            "run": row["run_ref"],
            "units": json.dumps(units or {}),
            "a": actual,
            "cur": row["currency"],
            "basis": cost_basis,
            "pv": pricing_version,
            "billed": billed_to,
            "n": note,
        },
    )
    return Reservation.from_row(updated, created=False)


def release(db: Session, tenant_id: UUID, reservation_id: UUID, *, note: str | None = None) -> Reservation:
    """Give back a reservation for work that definitely did not happen."""
    row = _locked(db, tenant_id, reservation_id)
    if row["state"] == "released":
        return Reservation.from_row(row, created=False)
    if row["state"] == "unknown":
        raise ConflictError("The outcome of this work is unknown. It must be resolved by a person, not released.")
    if row["state"] == "settled":
        raise ConflictError("A settled reservation cannot be released.")
    return _release(db, tenant_id, row, note)


def _release(db: Session, tenant_id: UUID, row: RowMapping, note: str | None) -> Reservation:
    db.execute(
        text("UPDATE budgets SET reserved_amount = reserved_amount - :a WHERE tenant_id = :t AND id = :b"),
        {"a": row["reserved_amount"], "t": tenant_id, "b": row["budget_id"]},
    )
    updated = (
        db.execute(
            text(
                "UPDATE budget_reservations SET state = 'released', settled_at = now(), note = COALESCE(:n, note) "
                "WHERE tenant_id = :t AND id = :r RETURNING *"
            ),
            {"n": note, "t": tenant_id, "r": row["id"]},
        )
        .mappings()
        .one()
    )
    return Reservation.from_row(updated, created=False)


def mark_unknown(db: Session, tenant_id: UUID, reservation_id: UUID, *, note: str) -> Reservation:
    """The provider call timed out or its result was lost. The budget stays held."""
    row = _locked(db, tenant_id, reservation_id)
    if row["state"] != "reserved":
        return Reservation.from_row(row, created=False)
    updated = (
        db.execute(
            text(
                "UPDATE budget_reservations SET state = 'unknown', note = :n WHERE tenant_id = :t AND id = :r RETURNING *"
            ),
            {"n": note, "t": tenant_id, "r": reservation_id},
        )
        .mappings()
        .one()
    )
    return Reservation.from_row(updated, created=False)


def resolve_unknown(
    db: Session, tenant_id: UUID, reservation_id: UUID, *, actual: Decimal | None, note: str, provider: str
) -> Reservation:
    """A person reconciles an unknown reservation: either with the real charge, or as not charged."""
    row = _locked(db, tenant_id, reservation_id)
    if row["state"] != "unknown":
        raise ConflictError("Only a reservation with an unknown outcome can be resolved.")
    if actual is None:
        return _release(db, tenant_id, row, note)
    return settle(
        db, tenant_id, reservation_id, actual=actual, cost_basis="provider_reported", provider=provider, note=note
    )
