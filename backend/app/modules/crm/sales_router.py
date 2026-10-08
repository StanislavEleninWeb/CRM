"""Leads, pipelines, deals and tasks.

Lead qualification and deal stage are separate lifecycles: a lead is qualified or
not; a deal moves through a pipeline. Converting a lead creates a deal.
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError

from app.core.deps import TenantContext, tenant_with
from app.core.errors import ConflictError
from app.core.pagination import Page, PageParams, page_params
from app.modules.crm.common import (
    ValidationFailed,
    check_owner,
    exists,
    log_activity,
    many,
    one,
    order_by,
    scalar,
    set_clause,
    validate_custom,
)
from app.modules.crm.schemas import (
    ConvertLead,
    DealIn,
    DealOut,
    DealUpdate,
    LeadIn,
    LeadOut,
    LeadStatus,
    LeadUpdate,
    Money,
    PipelineIn,
    PipelineOut,
    StageChangeOut,
    StageIn,
    StageOut,
    StageUpdate,
    TaskIn,
    TaskOut,
    TaskStatus,
    TaskUpdate,
)
from app.modules.identity.permissions import Permission

router = APIRouter()
Paging = Annotated[PageParams, Depends(page_params)]
READ = tenant_with(Permission.CRM_READ)
WRITE = tenant_with(Permission.CRM_WRITE)
MANAGE = tenant_with(Permission.PIPELINE_MANAGE)

LEAD_SELECT = "SELECT l.*, c.name AS company_name FROM leads l JOIN companies c ON c.tenant_id = l.tenant_id AND c.id = l.company_id"
DEAL_SELECT = """
    SELECT d.*, c.name AS company_name, s.name AS stage_name, s.kind AS stage_kind
    FROM deals d
    JOIN companies c ON c.tenant_id = d.tenant_id AND c.id = d.company_id
    JOIN pipeline_stages s ON s.tenant_id = d.tenant_id AND s.id = d.stage_id
"""
TASK_SELECT = "SELECT t.*, c.name AS company_name FROM tasks t LEFT JOIN companies c ON c.tenant_id = t.tenant_id AND c.id = t.company_id"


# --- leads -----------------------------------------------------------------------------------


def _lead(ctx: TenantContext, lead_id: UUID, *, lock: bool = False) -> RowMapping:
    suffix = "FOR UPDATE OF l" if lock else ""
    return one(
        ctx,
        f"{LEAD_SELECT} WHERE l.tenant_id = :tenant_id AND l.id = :id {suffix}",
        {"id": lead_id},
        "Lead not found.",
    )


@router.get("/leads", response_model=Page[LeadOut], operation_id="listLeads", tags=["leads"])
def list_leads(
    paging: Paging,
    ctx: TenantContext = READ,
    status: LeadStatus | None = None,
    owner_user_id: UUID | None = None,
    company_id: UUID | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    sort: Annotated[str | None, Query(max_length=40)] = None,
) -> Page[LeadOut]:
    where = ["l.tenant_id = :tenant_id"]
    params_typed: dict[str, Any] = {}
    for column, value in (("status", status), ("owner_user_id", owner_user_id), ("company_id", company_id)):
        if value is not None:
            where.append(f"l.{column} = :{column}")
            params_typed[column] = value
    if q:
        where.append("(lower(c.name) LIKE :q OR l.external_id ILIKE :q_exact)")
        escaped = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params_typed |= {"q": f"%{escaped}%", "q_exact": q}
    condition = " AND ".join(where)
    total = scalar(
        ctx,
        f"SELECT count(*) FROM leads l JOIN companies c ON c.tenant_id = l.tenant_id AND c.id = l.company_id WHERE {condition}",
        params_typed,
    )
    ordering = order_by(
        sort,
        {
            "created_at": "l.created_at",
            "company": "lower(c.name)",
            "next_action_at": "l.next_action_at",
            "status": "l.status",
        },
        "l.created_at DESC, l.id",
    )
    items = many(
        ctx,
        f"{LEAD_SELECT} WHERE {condition} ORDER BY {ordering} LIMIT :limit OFFSET :offset",
        {**params_typed, "limit": paging.limit, "offset": paging.offset},
    )
    return Page(items=[LeadOut(**row) for row in items], total=total, limit=paging.limit, offset=paging.offset)


@router.post("/leads", response_model=LeadOut, status_code=201, operation_id="createLead", tags=["leads"])
def create_lead(body: LeadIn, ctx: TenantContext = WRITE) -> LeadOut:
    exists(ctx, "companies", body.company_id, "The company")
    exists(ctx, "contacts", body.contact_id, "The contact")
    check_owner(ctx, body.owner_user_id)
    try:
        lead_id = scalar(
            ctx,
            """
            INSERT INTO leads (tenant_id, company_id, contact_id, external_id, status, owner_user_id,
                               source, next_action, next_action_at, custom)
            VALUES (:tenant_id, :company_id, :contact_id, :external_id, :status, :owner_user_id,
                    :source, :next_action, :next_action_at, CAST(:custom AS jsonb))
            RETURNING id
            """,
            {**body.model_dump(exclude={"custom"}), "custom": validate_custom(ctx, "lead", body.custom)},
        )
    except IntegrityError as exc:
        raise ConflictError("A lead with that external ID already exists.") from exc
    log_activity(ctx, "lead.created", "Lead created", company_id=body.company_id, lead_id=lead_id)
    return LeadOut(**_lead(ctx, lead_id))


@router.get("/leads/{lead_id}", response_model=LeadOut, operation_id="getLead", tags=["leads"])
def get_lead(lead_id: UUID, ctx: TenantContext = READ) -> LeadOut:
    return LeadOut(**_lead(ctx, lead_id))


@router.patch("/leads/{lead_id}", response_model=LeadOut, operation_id="updateLead", tags=["leads"])
def update_lead(lead_id: UUID, body: LeadUpdate, ctx: TenantContext = WRITE) -> LeadOut:
    current = _lead(ctx, lead_id, lock=True)
    changes = body.model_dump(exclude_unset=True)
    if current["status"] == "converted" and "status" in changes:
        raise ConflictError("A converted lead keeps its status. Work on the deal instead.")
    if "owner_user_id" in changes and changes["owner_user_id"] != current["owner_user_id"]:
        check_owner(ctx, changes["owner_user_id"])
    if "contact_id" in changes:
        exists(ctx, "contacts", changes["contact_id"], "The contact")
    if changes.get("status") and changes["status"] != "disqualified":
        changes["disqualify_reason"] = None
    assignments = []
    if "custom" in changes:
        changes["custom"] = validate_custom(ctx, "lead", changes["custom"])
        assignments.append("custom = CAST(:custom AS jsonb)")
    plain = {k: v for k, v in changes.items() if k != "custom"}
    if plain:
        assignments.append(set_clause(plain))
    if assignments:
        ctx.db.execute(
            text(f"UPDATE leads SET {', '.join(assignments)} WHERE tenant_id = :t AND id = :id"),
            {**changes, "t": ctx.tenant_id, "id": lead_id},
        )
        if "status" in changes and changes["status"] != current["status"]:
            log_activity(
                ctx,
                "lead.status_changed",
                f"Lead {changes['status'].replace('_', ' ')}",
                company_id=current["company_id"],
                lead_id=lead_id,
                data={"from": current["status"], "to": changes["status"], "reason": body.disqualify_reason},
            )
    return LeadOut(**_lead(ctx, lead_id))


@router.post(
    "/leads/{lead_id}/convert", response_model=DealOut, status_code=201, operation_id="convertLead", tags=["leads"]
)
def convert_lead(lead_id: UUID, body: ConvertLead, ctx: TenantContext = WRITE) -> DealOut:
    lead = _lead(ctx, lead_id, lock=True)
    if lead["status"] == "converted":
        raise ConflictError("This lead was already converted.")
    if lead["status"] == "disqualified":
        raise ConflictError("A disqualified lead cannot be converted.")
    deal = _insert_deal(
        ctx,
        DealIn(
            company_id=lead["company_id"],
            lead_id=lead_id,
            contact_id=lead["contact_id"],
            owner_user_id=lead["owner_user_id"],
            next_action=lead["next_action"],
            next_action_at=lead["next_action_at"],
            **body.model_dump(),
        ),
        check_assignment=False,
    )
    ctx.db.execute(
        text("UPDATE leads SET status = 'converted', converted_deal_id = :d WHERE tenant_id = :t AND id = :id"),
        {"d": deal.id, "t": ctx.tenant_id, "id": lead_id},
    )
    log_activity(
        ctx,
        "lead.converted",
        f"Lead converted to opportunity: {deal.title}",
        company_id=lead["company_id"],
        lead_id=lead_id,
        deal_id=deal.id,
    )
    return deal


# --- pipelines -------------------------------------------------------------------------------


def _pipelines(ctx: TenantContext, pipeline_id: UUID | None = None) -> list[PipelineOut]:
    stages = many(
        ctx,
        "SELECT id, pipeline_id, name, position, kind FROM pipeline_stages "
        "WHERE tenant_id = :tenant_id ORDER BY position, name",
    )
    pipelines = many(
        ctx,
        "SELECT id, name, is_default FROM pipelines WHERE tenant_id = :tenant_id "
        "AND (CAST(:p AS uuid) IS NULL OR id = :p) ORDER BY is_default DESC, lower(name)",
        {"p": pipeline_id},
    )
    return [PipelineOut(**p, stages=[StageOut(**s) for s in stages if s["pipeline_id"] == p["id"]]) for p in pipelines]


@router.get("/pipelines", response_model=list[PipelineOut], operation_id="listPipelines", tags=["pipelines"])
def list_pipelines(ctx: TenantContext = READ) -> list[PipelineOut]:
    return _pipelines(ctx)


@router.post(
    "/pipelines", response_model=PipelineOut, status_code=201, operation_id="createPipeline", tags=["pipelines"]
)
def create_pipeline(body: PipelineIn, ctx: TenantContext = MANAGE) -> PipelineOut:
    try:
        pipeline_id = scalar(
            ctx, "INSERT INTO pipelines (tenant_id, name) VALUES (:tenant_id, :n) RETURNING id", {"n": body.name}
        )
    except IntegrityError as exc:
        raise ConflictError("A pipeline with that name already exists.") from exc
    for position, (name, kind) in enumerate((("New", "open"), ("Won", "won"), ("Lost", "lost")), start=1):
        ctx.db.execute(
            text(
                "INSERT INTO pipeline_stages (tenant_id, pipeline_id, name, position, kind) "
                "VALUES (:t, :p, :n, :pos, :k)"
            ),
            {"t": ctx.tenant_id, "p": pipeline_id, "n": name, "pos": position * 10, "k": kind},
        )
    return _pipelines(ctx, pipeline_id)[0]


@router.post(
    "/pipelines/{pipeline_id}/stages",
    response_model=PipelineOut,
    status_code=201,
    operation_id="createStage",
    tags=["pipelines"],
)
def create_stage(pipeline_id: UUID, body: StageIn, ctx: TenantContext = MANAGE) -> PipelineOut:
    exists(ctx, "pipelines", pipeline_id, "The pipeline")
    try:
        ctx.db.execute(
            text(
                """
                INSERT INTO pipeline_stages (tenant_id, pipeline_id, name, kind, position)
                VALUES (:t, :p, :name, :kind, COALESCE(:position,
                    (SELECT COALESCE(max(position), 0) + 10 FROM pipeline_stages
                     WHERE tenant_id = :t AND pipeline_id = :p AND kind = 'open')))
                """
            ),
            {"t": ctx.tenant_id, "p": pipeline_id, **body.model_dump()},
        )
    except IntegrityError as exc:
        raise ConflictError("This pipeline already has a stage with that name.") from exc
    return _pipelines(ctx, pipeline_id)[0]


@router.patch("/stages/{stage_id}", response_model=StageOut, operation_id="updateStage", tags=["pipelines"])
def update_stage(stage_id: UUID, body: StageUpdate, ctx: TenantContext = MANAGE) -> StageOut:
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if not changes:
        raise ValidationFailed("Nothing to change.")
    try:
        row = one(
            ctx,
            f"UPDATE pipeline_stages SET {set_clause(changes)} WHERE tenant_id = :tenant_id AND id = :id "
            "RETURNING id, pipeline_id, name, position, kind",
            {**changes, "id": stage_id},
            "Stage not found.",
        )
    except IntegrityError as exc:
        raise ConflictError("This pipeline already has a stage with that name.") from exc
    return StageOut(**row)


@router.delete("/stages/{stage_id}", status_code=204, operation_id="deleteStage", tags=["pipelines"])
def delete_stage(stage_id: UUID, ctx: TenantContext = MANAGE) -> None:
    stage = one(
        ctx,
        "SELECT id, pipeline_id, kind FROM pipeline_stages WHERE tenant_id = :tenant_id AND id = :id FOR UPDATE",
        {"id": stage_id},
        "Stage not found.",
    )
    in_use = scalar(ctx, "SELECT count(*) FROM deals WHERE tenant_id = :tenant_id AND stage_id = :id", {"id": stage_id})
    if in_use:
        raise ConflictError(f"{in_use} opportunities are in this stage. Move them first.")
    remaining = scalar(
        ctx,
        "SELECT count(*) FROM pipeline_stages WHERE tenant_id = :tenant_id AND pipeline_id = :p "
        "AND kind = :k AND id <> :id",
        {"p": stage["pipeline_id"], "k": stage["kind"], "id": stage_id},
    )
    if not remaining:
        raise ConflictError(f"A pipeline needs at least one {stage['kind']} stage.")
    ctx.db.execute(
        text("DELETE FROM pipeline_stages WHERE tenant_id = :t AND id = :id"), {"t": ctx.tenant_id, "id": stage_id}
    )


# --- deals -----------------------------------------------------------------------------------


def _deal(ctx: TenantContext, deal_id: UUID, *, lock: bool = False) -> RowMapping:
    suffix = "FOR UPDATE OF d" if lock else ""
    return one(
        ctx,
        f"{DEAL_SELECT} WHERE d.tenant_id = :tenant_id AND d.id = :id {suffix}",
        {"id": deal_id},
        "Opportunity not found.",
    )


def _money(ctx: TenantContext, money: Money) -> dict[str, Any]:
    """Amounts default to the tenant currency. Original amounts keep their own currency."""
    values = money.model_dump(
        include={
            "amount",
            "currency",
            "original_amount",
            "original_currency",
            "conversion_rate",
            "conversion_source",
            "conversion_date",
        }
    )
    if values["amount"] is not None and values["currency"] is None:
        values["currency"] = scalar(ctx, "SELECT currency FROM tenants WHERE id = :tenant_id")
    if values["amount"] is None:
        values["currency"] = None
    return values


def _insert_deal(ctx: TenantContext, body: DealIn, *, check_assignment: bool = True) -> DealOut:
    exists(ctx, "companies", body.company_id, "The company")
    exists(ctx, "contacts", body.contact_id, "The contact")
    if check_assignment:
        check_owner(ctx, body.owner_user_id)
    pipeline_id = body.pipeline_id or scalar(
        ctx, "SELECT id FROM pipelines WHERE tenant_id = :tenant_id ORDER BY is_default DESC, created_at LIMIT 1"
    )
    if pipeline_id is None:
        raise ValidationFailed("Create a pipeline first.")
    exists(ctx, "pipelines", pipeline_id, "The pipeline")
    stage_id = body.stage_id or scalar(
        ctx,
        "SELECT id FROM pipeline_stages WHERE tenant_id = :tenant_id AND pipeline_id = :p AND kind = 'open' "
        "ORDER BY position LIMIT 1",
        {"p": pipeline_id},
    )
    belongs = scalar(
        ctx,
        "SELECT kind FROM pipeline_stages WHERE tenant_id = :tenant_id AND pipeline_id = :p AND id = :s",
        {"p": pipeline_id, "s": stage_id},
    )
    if belongs is None:
        raise ValidationFailed("That stage does not belong to the pipeline.")
    deal_id = scalar(
        ctx,
        """
        INSERT INTO deals (tenant_id, company_id, lead_id, contact_id, pipeline_id, stage_id, title, amount,
                           currency, original_amount, original_currency, conversion_rate, conversion_source,
                           conversion_date, expected_close_date, owner_user_id, next_action, next_action_at,
                           custom, closed_at)
        VALUES (:tenant_id, :company_id, :lead_id, :contact_id, :pipeline_id, :stage_id, :title, :amount,
                :currency, :original_amount, :original_currency, :conversion_rate, :conversion_source,
                :conversion_date, :expected_close_date, :owner_user_id, :next_action, :next_action_at,
                CAST(:custom AS jsonb), CASE WHEN :stage_kind = 'open' THEN NULL ELSE now() END)
        RETURNING id
        """,
        {
            **body.model_dump(exclude={"custom", "pipeline_id", "stage_id"}),
            **_money(ctx, body),
            "pipeline_id": pipeline_id,
            "stage_id": stage_id,
            "stage_kind": belongs,
            "custom": validate_custom(ctx, "deal", body.custom),
        },
    )
    ctx.db.execute(
        text("INSERT INTO deal_stage_changes (tenant_id, deal_id, to_stage_id, changed_by) VALUES (:t, :d, :s, :u)"),
        {"t": ctx.tenant_id, "d": deal_id, "s": stage_id, "u": ctx.user_id},
    )
    log_activity(ctx, "deal.created", f"Opportunity created: {body.title}", company_id=body.company_id, deal_id=deal_id)
    return DealOut(**_deal(ctx, deal_id))


@router.get("/deals", response_model=Page[DealOut], operation_id="listDeals", tags=["deals"])
def list_deals(
    paging: Paging,
    ctx: TenantContext = READ,
    pipeline_id: UUID | None = None,
    stage_id: UUID | None = None,
    company_id: UUID | None = None,
    owner_user_id: UUID | None = None,
    open_only: bool = False,
    sort: Annotated[str | None, Query(max_length=40)] = None,
) -> Page[DealOut]:
    where = ["d.tenant_id = :tenant_id"]
    typed: dict[str, Any] = {}
    for column, value in (
        ("pipeline_id", pipeline_id),
        ("stage_id", stage_id),
        ("company_id", company_id),
        ("owner_user_id", owner_user_id),
    ):
        if value is not None:
            where.append(f"d.{column} = :{column}")
            typed[column] = value
    if open_only:
        where.append("s.kind = 'open'")
    condition = " AND ".join(where)
    total = scalar(
        ctx,
        f"SELECT count(*) FROM deals d JOIN pipeline_stages s ON s.tenant_id = d.tenant_id AND s.id = d.stage_id WHERE {condition}",
        typed,
    )
    ordering = order_by(
        sort,
        {
            "created_at": "d.created_at",
            "amount": "d.amount",
            "expected_close_date": "d.expected_close_date",
            "next_action_at": "d.next_action_at",
            "title": "lower(d.title)",
        },
        "d.created_at DESC, d.id",
    ).replace(", id", ", d.id")
    items = many(
        ctx,
        f"{DEAL_SELECT} WHERE {condition} ORDER BY {ordering} LIMIT :limit OFFSET :offset",
        {**typed, "limit": paging.limit, "offset": paging.offset},
    )
    return Page(items=[DealOut(**row) for row in items], total=total, limit=paging.limit, offset=paging.offset)


@router.post("/deals", response_model=DealOut, status_code=201, operation_id="createDeal", tags=["deals"])
def create_deal(body: DealIn, ctx: TenantContext = WRITE) -> DealOut:
    exists(ctx, "leads", body.lead_id, "The lead")
    return _insert_deal(ctx, body)


@router.get("/deals/{deal_id}", response_model=DealOut, operation_id="getDeal", tags=["deals"])
def get_deal(deal_id: UUID, ctx: TenantContext = READ) -> DealOut:
    return DealOut(**_deal(ctx, deal_id))


@router.patch("/deals/{deal_id}", response_model=DealOut, operation_id="updateDeal", tags=["deals"])
def update_deal(deal_id: UUID, body: DealUpdate, ctx: TenantContext = WRITE) -> DealOut:
    current = _deal(ctx, deal_id, lock=True)
    changes = body.model_dump(exclude_unset=True)
    money_fields = {
        "amount",
        "currency",
        "original_amount",
        "original_currency",
        "conversion_rate",
        "conversion_source",
        "conversion_date",
    }
    if money_fields & changes.keys():
        changes |= _money(ctx, body)
    if "owner_user_id" in changes and changes["owner_user_id"] != current["owner_user_id"]:
        check_owner(ctx, changes["owner_user_id"])
    if "contact_id" in changes:
        exists(ctx, "contacts", changes["contact_id"], "The contact")
    if changes.get("title", "x") is None:
        raise ValidationFailed("An opportunity needs a title.")

    assignments: list[str] = []
    new_stage = None
    if changes.get("stage_id") and changes["stage_id"] != current["stage_id"]:
        new_stage = one(
            ctx,
            "SELECT id, name, kind FROM pipeline_stages WHERE tenant_id = :tenant_id AND pipeline_id = :p AND id = :s",
            {"p": current["pipeline_id"], "s": changes["stage_id"]},
            "That stage does not belong to this opportunity's pipeline.",
        )
        if new_stage["kind"] == "lost" and not (changes.get("loss_reason") or current["loss_reason"]):
            raise ValidationFailed("Give a reason when marking an opportunity lost.")
        assignments.append("closed_at = " + ("NULL" if new_stage["kind"] == "open" else "now()"))
        if new_stage["kind"] != "lost":
            changes["loss_reason"] = None
    else:
        changes.pop("stage_id", None)
    if "custom" in changes:
        changes["custom"] = validate_custom(ctx, "deal", changes["custom"])
        assignments.append("custom = CAST(:custom AS jsonb)")
    plain = {k: v for k, v in changes.items() if k != "custom"}
    if plain:
        assignments.append(set_clause(plain))
    if not assignments:
        return DealOut(**current)
    ctx.db.execute(
        text(f"UPDATE deals SET {', '.join(assignments)} WHERE tenant_id = :t AND id = :id"),
        {**changes, "t": ctx.tenant_id, "id": deal_id},
    )
    if new_stage is not None:
        ctx.db.execute(
            text(
                "INSERT INTO deal_stage_changes (tenant_id, deal_id, from_stage_id, to_stage_id, changed_by) "
                "VALUES (:t, :d, :f, :s, :u)"
            ),
            {"t": ctx.tenant_id, "d": deal_id, "f": current["stage_id"], "s": new_stage["id"], "u": ctx.user_id},
        )
        log_activity(
            ctx,
            "deal.stage_changed",
            f"Stage: {current['stage_name']} → {new_stage['name']}",
            company_id=current["company_id"],
            deal_id=deal_id,
            data={"from": current["stage_name"], "to": new_stage["name"], "loss_reason": changes.get("loss_reason")},
        )
    elif {"next_action", "next_action_at"} & changes.keys():
        log_activity(ctx, "deal.next_action", "Next action updated", company_id=current["company_id"], deal_id=deal_id)
    return DealOut(**_deal(ctx, deal_id))


@router.get(
    "/deals/{deal_id}/stage-history",
    response_model=list[StageChangeOut],
    operation_id="listDealStageHistory",
    tags=["deals"],
)
def deal_stage_history(deal_id: UUID, ctx: TenantContext = READ) -> list[StageChangeOut]:
    _deal(ctx, deal_id)
    rows = many(
        ctx,
        """
        SELECT h.id, h.from_stage_id, h.to_stage_id, COALESCE(s.name, 'Deleted stage') AS to_stage_name,
               h.changed_by, h.created_at
        FROM deal_stage_changes h
        LEFT JOIN pipeline_stages s ON s.tenant_id = h.tenant_id AND s.id = h.to_stage_id
        WHERE h.tenant_id = :tenant_id AND h.deal_id = :d ORDER BY h.created_at, h.id
        """,
        {"d": deal_id},
    )
    return [StageChangeOut(**row) for row in rows]


# --- tasks -----------------------------------------------------------------------------------


def _task(ctx: TenantContext, task_id: UUID) -> RowMapping:
    return one(ctx, f"{TASK_SELECT} WHERE t.tenant_id = :tenant_id AND t.id = :id", {"id": task_id}, "Task not found.")


@router.get("/tasks", response_model=Page[TaskOut], operation_id="listTasks", tags=["tasks"])
def list_tasks(
    paging: Paging,
    ctx: TenantContext = READ,
    status: TaskStatus | None = "open",
    assignee_user_id: UUID | None = None,
    mine: bool = False,
    company_id: UUID | None = None,
    deal_id: UUID | None = None,
    lead_id: UUID | None = None,
    due_before: datetime | None = None,
) -> Page[TaskOut]:
    where = ["t.tenant_id = :tenant_id"]
    params: dict[str, Any] = {}
    if mine:
        assignee_user_id = ctx.user_id
    for column, value in (
        ("status", status),
        ("assignee_user_id", assignee_user_id),
        ("company_id", company_id),
        ("deal_id", deal_id),
        ("lead_id", lead_id),
    ):
        if value is not None:
            where.append(f"t.{column} = :{column}")
            params[column] = value
    if due_before is not None:
        where.append("t.due_at <= :due_before")
        params["due_before"] = due_before
    condition = " AND ".join(where)
    total = scalar(ctx, f"SELECT count(*) FROM tasks t WHERE {condition}", params)
    items = many(
        ctx,
        f"{TASK_SELECT} WHERE {condition} ORDER BY t.due_at ASC NULLS LAST, t.created_at, t.id LIMIT :limit OFFSET :offset",
        {**params, "limit": paging.limit, "offset": paging.offset},
    )
    return Page(items=[TaskOut(**row) for row in items], total=total, limit=paging.limit, offset=paging.offset)


@router.post("/tasks", response_model=TaskOut, status_code=201, operation_id="createTask", tags=["tasks"])
def create_task(body: TaskIn, ctx: TenantContext = WRITE) -> TaskOut:
    for table, value, label in (
        ("companies", body.company_id, "The company"),
        ("contacts", body.contact_id, "The contact"),
        ("leads", body.lead_id, "The lead"),
        ("deals", body.deal_id, "The opportunity"),
    ):
        exists(ctx, table, value, label)
    assignee = body.assignee_user_id or ctx.user_id
    check_owner(ctx, assignee)
    task_id = scalar(
        ctx,
        """
        INSERT INTO tasks (tenant_id, title, description, kind, due_at, assignee_user_id, created_by,
                           company_id, contact_id, lead_id, deal_id)
        VALUES (:tenant_id, :title, :description, :kind, :due_at, :assignee, :actor,
                :company_id, :contact_id, :lead_id, :deal_id)
        RETURNING id
        """,
        {**body.model_dump(exclude={"assignee_user_id"}), "assignee": assignee, "actor": ctx.user_id},
    )
    return TaskOut(**_task(ctx, task_id))


@router.patch("/tasks/{task_id}", response_model=TaskOut, operation_id="updateTask", tags=["tasks"])
def update_task(task_id: UUID, body: TaskUpdate, ctx: TenantContext = WRITE) -> TaskOut:
    current = one(
        ctx,
        "SELECT * FROM tasks WHERE tenant_id = :tenant_id AND id = :id FOR UPDATE",
        {"id": task_id},
        "Task not found.",
    )
    changes = body.model_dump(exclude_unset=True)
    if changes.get("title", "x") is None:
        raise ValidationFailed("A task needs a title.")
    if "assignee_user_id" in changes and changes["assignee_user_id"] != current["assignee_user_id"]:
        check_owner(ctx, changes["assignee_user_id"])
    extra = ""
    if changes.get("status") and changes["status"] != current["status"]:
        extra = ", completed_at = " + ("now()" if changes["status"] == "done" else "NULL")
        if changes["status"] == "done":
            log_activity(
                ctx,
                "task.completed",
                f"Task completed: {current['title']}",
                company_id=current["company_id"],
                lead_id=current["lead_id"],
                deal_id=current["deal_id"],
                contact_id=current["contact_id"],
            )
    if changes:
        ctx.db.execute(
            text(f"UPDATE tasks SET {set_clause(changes)}{extra} WHERE tenant_id = :t AND id = :id"),
            {**changes, "t": ctx.tenant_id, "id": task_id},
        )
    return TaskOut(**_task(ctx, task_id))
