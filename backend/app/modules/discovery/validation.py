"""Validation of model output before anything is stored.

A model proposes; this module decides. Every observation must quote text that is really on
a page that was fetched, every contact must literally appear in fetched content, scores
must fit the rubric, and the recommended service must be one the tenant offers. Anything
else is dropped and recorded, and the candidate goes to human review. Model output can
never trigger an action: unknown keys such as ``actions`` are ignored and reported.
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.core.normalize import normalize_email, normalize_phone
from app.modules.research.scoring import Rubric, ScoreError, compute_score

ALLOWED_KEYS = {
    "observations",
    "hypotheses",
    "components",
    "component_reasons",
    "recommended_service",
    "confidence",
    "contacts",
    "opening",
    "discovery_question",
}


class Observation(BaseModel):
    text: str = Field(min_length=5, max_length=1000)
    evidence_url: str | None = None
    quote: str | None = Field(default=None, max_length=500)


class Contact(BaseModel):
    kind: Literal["phone", "email"]
    value: str = Field(min_length=3, max_length=254)
    source_url: str | None = None


class Proposal(BaseModel):
    model_config = {"extra": "ignore"}
    observations: list[Observation] = Field(default_factory=list, max_length=10)
    hypotheses: list[str] = Field(default_factory=list, max_length=10)
    components: dict[str, Any] = Field(default_factory=dict)
    component_reasons: dict[str, str] = Field(default_factory=dict)
    recommended_service: str | None = Field(default=None, max_length=200)
    confidence: Literal["high", "medium", "low"] = "low"
    contacts: list[Contact] = Field(default_factory=list, max_length=20)
    opening: str | None = Field(default=None, max_length=2000)
    discovery_question: str | None = Field(default=None, max_length=1000)


def _squash(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def validate_proposal(
    raw: Any, *, pages: list[dict[str, Any]], services: list[str], rubric: Rubric, country: str | None
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Return ``(clean proposal, issues)``. Any issue sends the candidate to review."""
    issues: list[dict[str, str]] = []

    def issue(code: str, message: str) -> None:
        issues.append({"code": code, "message": message})

    if not isinstance(raw, dict):
        return {}, [{"code": "not_an_object", "message": "The model did not return a structured proposal."}]
    extra = sorted(set(raw) - ALLOWED_KEYS)
    if extra:
        issue("unexpected_fields", f"Ignored fields the model is not allowed to set: {', '.join(extra)}.")
    try:
        proposal = Proposal.model_validate(raw)
    except ValidationError as exc:
        return {}, [
            *issues,
            {
                "code": "schema",
                "message": f"The proposal did not match the expected structure ({exc.error_count()} problems).",
            },
        ]

    page_text = {p["url"]: _squash(p.get("text", "")) for p in pages if p.get("url")}
    all_text = " ".join(page_text.values())
    page_contacts = {_digits(v) for p in pages for v in p.get("phones", [])} | {
        e.lower() for p in pages for e in p.get("emails", [])
    }

    observations = []
    for obs in proposal.observations:
        if obs.evidence_url not in page_text:
            issue("unsupported_evidence", "An observation cited a page that was not fetched; it was dropped.")
            continue
        if not obs.quote or _squash(obs.quote) not in page_text[obs.evidence_url]:
            issue("unsupported_quote", "An observation's quote does not appear on the cited page; it was dropped.")
            continue
        observations.append({"text": obs.text, "evidence_url": obs.evidence_url, "quote": obs.quote})

    contacts = []
    for contact in proposal.contacts:
        if contact.kind == "email":
            normalized = normalize_email(contact.value)
            present = normalized is not None and (normalized in page_contacts or normalized in all_text)
        else:
            normalized, _ = normalize_phone(contact.value, country)
            present = normalized is not None and (
                _digits(contact.value) in page_contacts or _digits(contact.value)[-7:] in _digits(all_text)
            )
        if not present:
            issue("fabricated_contact", f"A {contact.kind} that does not appear on any fetched page was rejected.")
            continue
        contacts.append(
            {
                "kind": contact.kind,
                "value": contact.value,
                "source_url": contact.source_url if contact.source_url in page_text else None,
            }
        )

    score = None
    try:
        computed = compute_score({k: v for k, v in proposal.components.items()}, rubric)
        if computed is not None:
            score = {"components": computed.components, "total": computed.total, "tier": computed.tier}
    except ScoreError as exc:
        issue("invalid_score", f"The proposed score was rejected: {exc}.")

    service = proposal.recommended_service
    if service and services and service not in services:
        issue("unknown_service", "The recommended service is not in this workspace's catalogue; it was dropped.")
        service = None

    confidence = proposal.confidence
    if confidence == "high" and (not observations or issues):
        confidence = "low" if not observations else "medium"  # confidence cannot exceed what the evidence supports
    if not observations:
        issue("no_evidence", "No observation is backed by fetched page content.")

    clean = {
        "observations": observations,
        "hypotheses": [h.strip() for h in proposal.hypotheses if h.strip()][:10],
        "score": score,
        "component_reasons": {k: v[:300] for k, v in proposal.component_reasons.items() if k in rubric.keys},
        "recommended_service": service,
        "confidence": confidence,
        "contacts": contacts,
        "opening": proposal.opening,
        "discovery_question": proposal.discovery_question,
    }
    return clean, issues


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)
