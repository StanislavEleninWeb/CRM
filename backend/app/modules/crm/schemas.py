from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

ChannelKind = Literal["phone", "email", "website", "contact_page", "social", "messaging", "other"]
ChannelPurpose = Literal["general", "booking", "delivery", "emergency", "unknown"]
Verification = Literal["unverified", "verified", "invalid"]
LeadStatus = Literal["discovered", "needs_review", "qualified", "disqualified", "converted"]
TaskKind = Literal["todo", "call", "follow_up", "email"]
TaskStatus = Literal["open", "done", "cancelled"]
EntityType = Literal["company", "contact", "lead", "deal"]
RestrictionKind = Literal["phone", "email", "any"]

Name = Field(min_length=1, max_length=300)
ShortText = Field(default=None, max_length=200)


class Strict(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}


# --- companies -------------------------------------------------------------------------------


class CompanyIn(Strict):
    name: str = Name
    external_id: str | None = Field(default=None, max_length=100)
    business_type: str | None = ShortText
    industry_group: str | None = ShortText
    city: str | None = ShortText
    country: str | None = ShortText
    language: str | None = Field(default=None, max_length=20)
    website_url: str | None = Field(default=None, max_length=500)
    owner_user_id: UUID | None = None
    next_action: str | None = Field(default=None, max_length=500)
    next_action_at: datetime | None = None
    custom: dict[str, Any] | None = None


class CompanyUpdate(CompanyIn):
    name: str | None = Field(default=None, min_length=1, max_length=300)  # type: ignore[assignment]
    archived: bool | None = None


class CompanyOut(BaseModel):
    id: UUID
    name: str
    external_id: str | None
    business_type: str | None
    industry_group: str | None
    city: str | None
    country: str | None
    language: str | None
    website_url: str | None
    domain: str | None
    owner_user_id: UUID | None
    source: str
    next_action: str | None
    next_action_at: datetime | None
    merged_into_id: UUID | None
    archived_at: datetime | None
    custom: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ChannelIn(Strict):
    kind: ChannelKind
    raw_value: str = Field(min_length=1, max_length=500)
    purpose: ChannelPurpose = "general"
    label: str | None = Field(default=None, max_length=120)
    contact_id: UUID | None = None
    source_url: str | None = Field(default=None, max_length=1000)
    source_date: date | None = None
    verification_state: Verification = "unverified"


class ChannelUpdate(Strict):
    purpose: ChannelPurpose | None = None
    label: str | None = Field(default=None, max_length=120)
    verification_state: Verification | None = None
    do_not_contact: bool | None = None
    restriction_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _reason_required(self) -> "ChannelUpdate":
        if self.do_not_contact and not self.restriction_reason:
            raise ValueError("a reason is required to mark a channel do-not-contact")
        return self


class ChannelOut(BaseModel):
    id: UUID
    company_id: UUID
    contact_id: UUID | None
    kind: ChannelKind
    purpose: ChannelPurpose
    raw_value: str
    normalized_value: str | None
    normalized_is_e164: bool
    label: str | None
    source_type: str
    source_url: str | None
    source_date: date | None
    verification_state: Verification
    do_not_contact: bool
    restriction_reason: str | None
    allow_sales_use: bool
    dial_uri: str | None = Field(
        default=None, description="tel: link, present only when the number may be used for sales calls"
    )


class RestrictionIn(Strict):
    channel_kind: RestrictionKind
    reason: str = Field(min_length=1, max_length=500)
    contact_id: UUID | None = None


class RestrictionLift(Strict):
    lift_reason: str = Field(min_length=1, max_length=500)


class RestrictionOut(BaseModel):
    id: UUID
    company_id: UUID
    contact_id: UUID | None
    channel_kind: RestrictionKind
    reason: str
    source: str
    created_by: UUID | None
    created_at: datetime
    lifted_at: datetime | None
    lift_reason: str | None


class ContactIn(Strict):
    full_name: str = Field(min_length=1, max_length=200)
    job_title: str | None = ShortText
    language: str | None = Field(default=None, max_length=20)


class ContactOut(BaseModel):
    id: UUID
    company_id: UUID
    full_name: str
    job_title: str | None
    language: str | None
    created_at: datetime


class CompanyDetail(CompanyOut):
    channels: list[ChannelOut]
    contacts: list[ContactOut]
    restrictions: list[RestrictionOut]
    tags: list[str]
    open_task_count: int
    lead_count: int
    deal_count: int


class DuplicateCandidate(BaseModel):
    company: CompanyOut
    reasons: list[str]
    name_similarity: float
    caution: str | None = Field(default=None, description="Why this may be a separate branch rather than a duplicate")


class MergeRequest(Strict):
    source_id: UUID = Field(description="The company that will be merged into this one and archived")
    confirm: Literal[True]


class BulkCompanies(Strict):
    ids: list[UUID] = Field(min_length=1, max_length=500)
    action: Literal["assign_owner", "add_tag", "archive", "unarchive"]
    owner_user_id: UUID | None = None
    tag: str | None = Field(default=None, min_length=1, max_length=120)
    dry_run: bool = True

    @model_validator(mode="after")
    def _arguments(self) -> "BulkCompanies":
        if self.action == "assign_owner" and self.owner_user_id is None:
            raise ValueError("owner_user_id is required")
        if self.action == "add_tag" and not self.tag:
            raise ValueError("tag is required")
        return self


class BulkResult(BaseModel):
    matched: int
    changed: int
    dry_run: bool
    missing: list[UUID]


# --- notes, tags, activities, files ----------------------------------------------------------


class Links(Strict):
    company_id: UUID | None = None
    contact_id: UUID | None = None
    lead_id: UUID | None = None
    deal_id: UUID | None = None


class NoteIn(Links):
    body: str = Field(min_length=1, max_length=20000)

    @model_validator(mode="after")
    def _needs_a_record(self) -> "NoteIn":
        if not (self.company_id or self.lead_id or self.deal_id or self.contact_id):
            raise ValueError("a note must be attached to a record")
        return self


class NoteOut(BaseModel):
    id: UUID
    body: str
    author_user_id: UUID | None
    company_id: UUID | None
    contact_id: UUID | None
    lead_id: UUID | None
    deal_id: UUID | None
    created_at: datetime


class TagsIn(Strict):
    tags: list[str] = Field(max_length=50)

    @field_validator("tags")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        cleaned = [tag.strip() for tag in value if tag.strip()]
        if any(len(tag) > 120 for tag in cleaned):
            raise ValueError("tags are limited to 120 characters")
        return list(dict.fromkeys(cleaned))


class ActivityOut(BaseModel):
    id: UUID
    occurred_at: datetime
    kind: str
    summary: str
    actor_type: str
    actor_user_id: UUID | None
    origin: str
    company_id: UUID | None
    contact_id: UUID | None
    lead_id: UUID | None
    deal_id: UUID | None
    data: dict[str, Any]


class AttachmentOut(BaseModel):
    id: UUID
    filename: str
    content_type: str
    size_bytes: int
    company_id: UUID | None
    lead_id: UUID | None
    deal_id: UUID | None
    uploaded_by: UUID | None
    created_at: datetime


# --- leads, pipelines, deals -----------------------------------------------------------------


class LeadIn(Strict):
    company_id: UUID
    contact_id: UUID | None = None
    external_id: str | None = Field(default=None, max_length=100)
    status: Literal["discovered", "needs_review", "qualified"] = "discovered"
    owner_user_id: UUID | None = None
    source: str = Field(default="manual", max_length=100)
    next_action: str | None = Field(default=None, max_length=500)
    next_action_at: datetime | None = None
    custom: dict[str, Any] | None = None


class LeadUpdate(Strict):
    contact_id: UUID | None = None
    status: Literal["discovered", "needs_review", "qualified", "disqualified"] | None = None
    disqualify_reason: str | None = Field(default=None, max_length=500)
    outreach_status: str | None = Field(default=None, max_length=60)
    owner_user_id: UUID | None = None
    next_action: str | None = Field(default=None, max_length=500)
    next_action_at: datetime | None = None
    custom: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _reason(self) -> "LeadUpdate":
        if self.status == "disqualified" and not self.disqualify_reason:
            raise ValueError("a reason is required to disqualify a lead")
        return self


class LeadOut(BaseModel):
    id: UUID
    company_id: UUID
    company_name: str
    contact_id: UUID | None
    external_id: str | None
    status: LeadStatus
    outreach_status: str
    owner_user_id: UUID | None
    source: str
    disqualify_reason: str | None
    next_action: str | None
    next_action_at: datetime | None
    converted_deal_id: UUID | None
    custom: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class Money(Strict):
    amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    # Conversion provenance: only when the amount was converted from another currency.
    original_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    original_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    conversion_rate: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=8)
    conversion_source: str | None = Field(default=None, max_length=200)
    conversion_date: date | None = None

    @model_validator(mode="after")
    def _conversion_is_complete(self) -> "Money":
        parts = (
            self.original_amount,
            self.original_currency,
            self.conversion_rate,
            self.conversion_source,
            self.conversion_date,
        )
        if any(part is not None for part in parts) and not all(part is not None for part in parts):
            raise ValueError("conversion needs original amount, currency, rate, source and date")
        if self.original_amount is not None and self.amount is None:
            raise ValueError("a converted amount is required when conversion details are given")
        return self


class ConvertLead(Money):
    title: str = Name
    pipeline_id: UUID | None = None
    expected_close_date: date | None = None


class DealIn(Money):
    company_id: UUID
    title: str = Name
    lead_id: UUID | None = None
    contact_id: UUID | None = None
    pipeline_id: UUID | None = None
    stage_id: UUID | None = None
    expected_close_date: date | None = None
    owner_user_id: UUID | None = None
    next_action: str | None = Field(default=None, max_length=500)
    next_action_at: datetime | None = None
    custom: dict[str, Any] | None = None


class DealUpdate(Money):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    contact_id: UUID | None = None
    stage_id: UUID | None = None
    expected_close_date: date | None = None
    owner_user_id: UUID | None = None
    next_action: str | None = Field(default=None, max_length=500)
    next_action_at: datetime | None = None
    loss_reason: str | None = Field(default=None, max_length=500)
    custom: dict[str, Any] | None = None


class DealOut(BaseModel):
    id: UUID
    company_id: UUID
    company_name: str
    lead_id: UUID | None
    contact_id: UUID | None
    pipeline_id: UUID
    stage_id: UUID
    stage_name: str
    stage_kind: Literal["open", "won", "lost"]
    title: str
    amount: Decimal | None
    currency: str | None
    original_amount: Decimal | None
    original_currency: str | None
    conversion_rate: Decimal | None
    conversion_source: str | None
    conversion_date: date | None
    expected_close_date: date | None
    owner_user_id: UUID | None
    next_action: str | None
    next_action_at: datetime | None
    loss_reason: str | None
    closed_at: datetime | None
    custom: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class StageChangeOut(BaseModel):
    id: UUID
    from_stage_id: UUID | None
    to_stage_id: UUID
    to_stage_name: str
    changed_by: UUID | None
    created_at: datetime


class StageIn(Strict):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["open", "won", "lost"] = "open"
    position: int | None = Field(default=None, ge=0, le=100000)


class StageUpdate(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    position: int | None = Field(default=None, ge=0, le=100000)


class StageOut(BaseModel):
    id: UUID
    pipeline_id: UUID
    name: str
    position: int
    kind: Literal["open", "won", "lost"]


class PipelineIn(Strict):
    name: str = Field(min_length=1, max_length=120)


class PipelineOut(BaseModel):
    id: UUID
    name: str
    is_default: bool
    stages: list[StageOut]


# --- tasks, views, custom fields -------------------------------------------------------------


class TaskIn(Links):
    title: str = Name
    description: str | None = Field(default=None, max_length=5000)
    kind: TaskKind = "todo"
    due_at: datetime | None = None
    assignee_user_id: UUID | None = None


class TaskUpdate(Strict):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    due_at: datetime | None = None
    assignee_user_id: UUID | None = None
    status: TaskStatus | None = None


class TaskOut(BaseModel):
    id: UUID
    title: str
    description: str | None
    kind: TaskKind
    status: TaskStatus
    due_at: datetime | None
    assignee_user_id: UUID | None
    created_by: UUID | None
    completed_at: datetime | None
    company_id: UUID | None
    company_name: str | None
    contact_id: UUID | None
    lead_id: UUID | None
    deal_id: UUID | None
    created_at: datetime


class SavedViewIn(Strict):
    entity_type: Literal["company", "lead", "deal", "task", "prospect"]
    name: str = Field(min_length=1, max_length=120)
    is_shared: bool = False
    filters: dict[str, str | int | bool | list[str] | None] = Field(default_factory=dict)
    sort: str | None = Field(default=None, max_length=60)

    @field_validator("filters")
    @classmethod
    def _bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 30:
            raise ValueError("too many filters")
        return value


class SavedViewOut(BaseModel):
    id: UUID
    entity_type: str
    name: str
    is_shared: bool
    owner_user_id: UUID | None
    filters: dict[str, Any]
    sort: str | None


class CustomFieldIn(Strict):
    entity_type: EntityType
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    label: str = Field(min_length=1, max_length=120)
    field_type: Literal["text", "number", "date", "boolean", "select"]
    options: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _options(self) -> "CustomFieldIn":
        if self.field_type == "select" and not self.options:
            raise ValueError("a select field needs options")
        if self.field_type != "select" and self.options:
            raise ValueError("only select fields have options")
        return self


class CustomFieldOut(BaseModel):
    id: UUID
    entity_type: EntityType
    key: str
    label: str
    field_type: str
    options: list[str]
