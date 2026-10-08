"""Normalisation of contact values. Raw input is always stored alongside."""

import re

_EMAIL = re.compile(
    r"^[^@\s<>()\[\],;:\\\"]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$"
)


def normalize_email(value: str) -> str | None:
    """Lower-cased address, or None when the text is not a plausible email.

    Syntax only: no deliverability or reserved-domain policy is applied.
    """
    candidate = value.strip().lower()
    if len(candidate) > 254 or ".." in candidate or not _EMAIL.match(candidate):
        return None
    return candidate
