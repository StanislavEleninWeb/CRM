"""Deterministic prospect scoring.

The total and the tier are always computed here, never taken from input. A score is a
prioritisation heuristic: it is not a probability of purchase. A lead with missing
components is *unscored*; missing is never treated as zero.
"""

from dataclasses import dataclass
from typing import Any

DEFAULT_COMPONENTS: tuple[tuple[str, str, int], ...] = (
    ("evidence", "Evidence strength", 30),
    ("relevance", "Relevance to our services", 25),
    ("value", "Plausible business value", 20),
    ("reachability", "Reachability", 15),
    ("activity", "Signs of current business activity", 10),
)


class ScoreError(ValueError):
    """The component values do not fit the rubric."""


@dataclass(frozen=True)
class Rubric:
    components: tuple[tuple[str, str, int], ...] = DEFAULT_COMPONENTS
    tier_a_min: int = 80
    tier_b_min: int = 60

    @classmethod
    def from_row(cls, row: Any) -> "Rubric":
        return cls(
            components=tuple((c["key"], c["label"], int(c["max"])) for c in row["components"]),
            tier_a_min=int(row["tier_a_min"]),
            tier_b_min=int(row["tier_b_min"]),
        )

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(key for key, _, _ in self.components)

    @property
    def maximum(self) -> int:
        return sum(cap for _, _, cap in self.components)

    def tier_for(self, total: int) -> str:
        if total >= self.tier_a_min:
            return "A"
        return "B" if total >= self.tier_b_min else "C"


@dataclass(frozen=True)
class Score:
    components: dict[str, int]
    total: int
    tier: str


def compute_score(components: dict[str, Any], rubric: Rubric) -> Score | None:
    """Validate component values and compute the total and tier.

    Returns ``None`` when every component is missing (the lead is unscored). Raises
    ``ScoreError`` when only some are missing or any value is out of range.
    """
    present = {key: components.get(key) for key in rubric.keys}
    unknown = sorted(set(components) - set(rubric.keys))
    if unknown:
        raise ScoreError(f"unknown score components: {', '.join(unknown)}")
    if all(value is None for value in present.values()):
        return None
    cleaned: dict[str, int] = {}
    for key, label, cap in rubric.components:
        value = present[key]
        if value is None:
            raise ScoreError(f"{label} is missing; a partial assessment cannot be scored")
        if isinstance(value, bool) or not isinstance(value, int | float) or value != int(value):
            raise ScoreError(f"{label} must be a whole number")
        number = int(value)
        if not 0 <= number <= cap:
            raise ScoreError(f"{label} must be between 0 and {cap}")
        cleaned[key] = number
    total = sum(cleaned.values())
    return Score(components=cleaned, total=total, tier=rubric.tier_for(total))
