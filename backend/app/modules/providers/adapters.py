"""Provider adapters.

An adapter knows how to check a credential and report health for one provider. Real
providers are added by the phases that use them. The ``fake_*`` adapters exist only for
local development and tests, are labelled as such everywhere, and are refused in
staging and production.
"""

from dataclasses import dataclass
from typing import Any, Protocol

from app.core.config import get_settings


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    message: str = ""
    rate_limited_for_seconds: int | None = None
    revoked: bool = False


class ProviderAdapter(Protocol):
    name: str
    purpose: str
    label: str
    access_modes: tuple[str, ...]
    is_fake: bool

    def validate(self, secret: str | None, config: dict[str, Any]) -> HealthResult: ...


class _Fake:
    """Deterministic stand-in. The secret's text decides the outcome so tests can drive it."""

    name: str
    purpose: str
    label: str
    access_modes: tuple[str, ...] = ("byok", "platform")
    is_fake: bool = True

    def __init__(self, name: str, purpose: str, label: str) -> None:
        self.name, self.purpose, self.label = name, purpose, label

    def validate(self, secret: str | None, config: dict[str, Any]) -> HealthResult:
        if not secret:
            return HealthResult(ok=False, message="No credential is stored.")
        if "revoked" in secret:
            return HealthResult(ok=False, message="The provider rejected this credential.", revoked=True)
        if "throttle" in secret:
            return HealthResult(
                ok=False, message="The provider is rate limiting requests.", rate_limited_for_seconds=120
            )
        if "broken" in secret:
            return HealthResult(ok=False, message="The provider could not be reached.")
        return HealthResult(ok=True, message="Local test adapter: no real provider was contacted.")


_REGISTRY: dict[str, ProviderAdapter] = {
    "fake_model": _Fake("fake_model", "model", "Local test model (not a real provider)"),
    "fake_discovery": _Fake("fake_discovery", "discovery", "Local test discovery (not a real provider)"),
}


def register_adapter(adapter: ProviderAdapter) -> None:
    _REGISTRY[adapter.name] = adapter


def available_adapters() -> list[ProviderAdapter]:
    production_like = get_settings().is_production_like
    return [a for a in _REGISTRY.values() if not (a.is_fake and production_like)]


def get_adapter(name: str) -> ProviderAdapter | None:
    return next((a for a in available_adapters() if a.name == name), None)
