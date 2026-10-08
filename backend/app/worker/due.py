"""Registry of due-row handlers polled by the scheduler.

Each handler claims at most ``batch_size`` rows that are due now, using
``FOR UPDATE SKIP LOCKED`` in a short transaction, and returns how many it
claimed. No handler may hold a row lock across a network call.
"""

from collections.abc import Callable

from app.core.logging import get_logger

log = get_logger(__name__)
DueHandler = Callable[[int], int]
_handlers: dict[str, DueHandler] = {}


def register_due_handler(name: str, handler: DueHandler) -> None:
    _handlers[name] = handler


def run_due_handlers(batch_size: int) -> int:
    claimed = 0
    for name, handler in _handlers.items():
        try:
            claimed += handler(batch_size)
        except Exception as exc:
            log.error("due_handler_failed", handler=name, exc_info=exc)
    return claimed
