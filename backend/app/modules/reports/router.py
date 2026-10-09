"""Conversion reporting and the daily workspace.

Every figure says what it counts and what it is divided by. A rate with nothing to divide by
is reported as "no data", never as zero. Figures entered by people are labelled as reported
by people; nothing here is confirmed by a telephone or mail provider unless it says so.
"""

from datetime import date, datetime, time, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.core.deps import TenantContext, tenant_with
from app.core.time import today_in, utcnow
from app.modules.crm.common import ValidationFailed, many, one, scalar
from app.modules.identity.permissions import Permission

router = APIRouter()
REPORTS = tenant_with(Permission.REPORTS_READ)
READ = tenant_with(Permission.CRM_READ)
STALE_DEAL_DAYS = 14


class Metric(BaseModel):
    key: str
    label: str
    value: float | None = Field(description="None means there is nothing to count or divide by: no data, not zero")
    unit: Literal["count", "percent", "hours", "days", "money"]
    numerator: int | None = None
    denominator: int | None = None
    definition: str
    basis: Literal["reported_by_people", "recorded_by_system", "from_mailbox", "estimated_and_verified_costs"]
    note: str | None = None
    drill: str | None = Field(default=None, description="The key to pass to /reports/records to list what was counted")


class MoneyLine(BaseModel):
    currency: str
    amount: str
    basis: str


class StageRow(BaseModel):
    stage: str
    kind: str
    entered_in_period: int
    open_now: int
    average_days_in_stage: float | None
    oldest_days_in_stage: float | None


class FunnelOut(BaseModel):
    date_from: date
    date_to: date
    timezone: str
    cohort_rule: str
    metrics: list[Metric]
    stages: list[StageRow]
    won_amounts: list[MoneyLine] = Field(description="One line per currency. Currencies are never added together.")
    research_costs: list[MoneyLine]
    not_collected: list[str]


class RecordOut(BaseModel):
    kind: str
    id: UUID
    label: str
    at: datetime | None
    link: str | None


def _window(
    ctx: TenantContext, date_from: date | None, date_to: date | None
) -> tuple[date, date, datetime, datetime, str]:
    zone_name = scalar(ctx, "SELECT timezone FROM tenants WHERE id = :tenant_id")
    zone = ZoneInfo(zone_name)
    end_day = date_to or today_in(zone_name)
    start_day = date_from or end_day - timedelta(days=29)
    if start_day > end_day:
        raise ValidationFailed("The start date is after the end date.")
    if (end_day - start_day).days > 366:
        raise ValidationFailed("Choose a period of at most a year.")
    # Whole local days: from midnight at the start to midnight after the end, in the workspace zone.
    start = datetime.combine(start_day, time.min, tzinfo=zone)
    end = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=zone)
    return start_day, end_day, start, end, zone_name


def _rate(numerator: int, denominator: int) -> float | None:
    return round(100 * numerator / denominator, 1) if denominator else None


# Each entry: what is listed when someone asks to see the records behind a figure.
RECORDS: dict[str, str] = {
    "qualified_leads": "SELECT 'lead' AS kind, l.id, c.name AS label, l.created_at AS at, '/prospects/' || l.id AS link FROM leads l "
    "JOIN companies c ON c.tenant_id = l.tenant_id AND c.id = l.company_id WHERE l.tenant_id = :tenant_id AND l.status = 'qualified' "
    "AND l.created_at >= :s AND l.created_at < :e",
    "dialler_launches": "SELECT 'call' AS kind, a.id, c.name || ' - ' || a.dialed_value AS label, a.launched_at AS at, "
    "'/prospects/' || a.lead_id AS link FROM call_attempts a JOIN companies c ON c.tenant_id = a.tenant_id AND c.id = a.company_id "
    "WHERE a.tenant_id = :tenant_id AND a.launched_at >= :s AND a.launched_at < :e",
    "reported_calls": "SELECT 'call' AS kind, a.id, c.name || ' - ' || a.outcome AS label, a.outcome_reported_at AS at, "
    "'/prospects/' || a.lead_id AS link FROM call_attempts a JOIN companies c ON c.tenant_id = a.tenant_id AND c.id = a.company_id "
    "WHERE a.tenant_id = :tenant_id AND a.outcome IS NOT NULL AND a.outcome_reported_at >= :s AND a.outcome_reported_at < :e",
    "connected_calls": "SELECT 'call' AS kind, a.id, c.name || ' - ' || a.outcome AS label, a.outcome_reported_at AS at, "
    "'/prospects/' || a.lead_id AS link FROM call_attempts a JOIN companies c ON c.tenant_id = a.tenant_id AND c.id = a.company_id "
    "WHERE a.tenant_id = :tenant_id AND a.outcome IN ('connected', 'follow_up_requested', 'not_interested') "
    "AND a.outcome_reported_at >= :s AND a.outcome_reported_at < :e",
    "follow_up_permissions": "SELECT 'call' AS kind, a.id, c.name || COALESCE(' - ' || a.follow_up_scope, '') AS label, "
    "a.outcome_reported_at AS at, '/prospects/' || a.lead_id AS link FROM call_attempts a JOIN companies c ON c.tenant_id = a.tenant_id "
    "AND c.id = a.company_id WHERE a.tenant_id = :tenant_id AND a.outcome = 'follow_up_requested' "
    "AND a.outcome_reported_at >= :s AND a.outcome_reported_at < :e",
    "callbacks_due": "SELECT 'task' AS kind, t.id, t.title AS label, t.due_at AS at, NULL AS link FROM tasks t "
    "WHERE t.tenant_id = :tenant_id AND t.kind = 'follow_up' AND t.status <> 'cancelled' AND t.due_at >= :s AND t.due_at < :e",
    "callbacks_completed": "SELECT 'task' AS kind, t.id, t.title AS label, t.completed_at AS at, NULL AS link FROM tasks t "
    "WHERE t.tenant_id = :tenant_id AND t.kind = 'follow_up' AND t.status = 'done' AND t.due_at >= :s AND t.due_at < :e",
    "replies": "SELECT 'email' AS kind, m.id, COALESCE(m.subject, '(no subject)') AS label, m.sent_at AS at, "
    "CASE WHEN th.lead_id IS NOT NULL THEN '/prospects/' || th.lead_id END AS link FROM email_messages m JOIN email_threads th "
    "ON th.tenant_id = m.tenant_id AND th.id = m.thread_id WHERE m.tenant_id = :tenant_id AND m.direction = 'inbound' "
    "AND m.classification = 'reply' AND m.sent_at >= :s AND m.sent_at < :e",
    "emails_accepted": "SELECT 'email' AS kind, i.id, i.to_address::text AS label, i.accepted_at AS at, NULL AS link FROM send_intents i "
    "WHERE i.tenant_id = :tenant_id AND i.state = 'provider_accepted' AND i.accepted_at >= :s AND i.accepted_at < :e",
    "wins": "SELECT 'deal' AS kind, d.id, d.title AS label, d.closed_at AS at, NULL AS link FROM deals d JOIN pipeline_stages st "
    "ON st.tenant_id = d.tenant_id AND st.id = d.stage_id WHERE d.tenant_id = :tenant_id AND st.kind = 'won' "
    "AND d.closed_at >= :s AND d.closed_at < :e",
}


def _count(ctx: TenantContext, key: str, window: dict[str, Any]) -> int:
    return int(scalar(ctx, f"SELECT count(*) FROM ({RECORDS[key]}) q", window))


@router.get("/reports/funnel", response_model=FunnelOut, operation_id="getFunnelReport", tags=["reports"])
def funnel(
    ctx: TenantContext = REPORTS,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
) -> FunnelOut:
    start_day, end_day, start, end, zone = _window(ctx, date_from, date_to)
    w = {"s": start, "e": end}
    n = {key: _count(ctx, key, w) for key in RECORDS}

    response = one(
        ctx,
        "SELECT count(*) AS answered, percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (o.first_out - m.sent_at)) / 3600) AS median "
        "FROM email_messages m JOIN LATERAL (SELECT min(x.sent_at) AS first_out FROM email_messages x WHERE x.tenant_id = m.tenant_id "
        "AND x.thread_id = m.thread_id AND x.direction = 'outbound' AND x.sent_at > m.sent_at) o ON o.first_out IS NOT NULL "
        "WHERE m.tenant_id = :tenant_id AND m.direction = 'inbound' AND m.classification = 'reply' AND m.sent_at >= :s AND m.sent_at < :e",
        w,
        "No data.",
    )
    research_leads = int(
        scalar(
            ctx,
            "SELECT count(*) FROM leads WHERE tenant_id = :tenant_id AND source = 'research' AND status = 'qualified' "
            "AND created_at >= :s AND created_at < :e",
            w,
        )
    )
    costs = many(
        ctx,
        "SELECT currency, cost_basis, sum(amount) AS amount FROM usage_ledger WHERE tenant_id = :tenant_id AND purpose LIKE 'research%' "
        "AND occurred_at >= :s AND occurred_at < :e GROUP BY 1, 2 ORDER BY 1, 2",
        w,
    )
    pending = many(
        ctx,
        "SELECT currency, state, sum(reserved_amount) AS amount FROM budget_reservations WHERE tenant_id = :tenant_id "
        "AND state IN ('reserved', 'unknown') AND created_at >= :s AND created_at < :e GROUP BY 1, 2 ORDER BY 1, 2",
        w,
    )
    cost_lines = [MoneyLine(currency=r["currency"], amount=str(r["amount"]), basis=r["cost_basis"]) for r in costs] + [
        MoneyLine(
            currency=r["currency"],
            amount=str(r["amount"]),
            basis="still reserved" if r["state"] == "reserved" else "outcome unknown: may or may not be charged",
        )
        for r in pending
    ]
    known_currencies = {r["currency"] for r in costs}
    known_total = sum((r["amount"] for r in costs), start=0) if len(known_currencies) == 1 else None
    cost_per_lead = (
        float(round(known_total / research_leads, 4)) if known_total is not None and research_leads else None
    )
    cost_note = None
    if len(known_currencies) > 1:
        cost_note = "Costs are in more than one currency and are not added together."
    elif pending:
        cost_note = "Reserved or unknown costs are listed separately and are not in this figure."

    metrics = [
        Metric(
            key="qualified_leads",
            label="Qualified leads",
            value=n["qualified_leads"],
            unit="count",
            drill="qualified_leads",
            definition="Leads created in the period whose qualification status is 'qualified' now. Qualification is separate from "
            "communication status and from opportunity stage.",
            basis="recorded_by_system",
        ),
        Metric(
            key="dialler_launches",
            label="Dialler opened",
            value=n["dialler_launches"],
            unit="count",
            drill="dialler_launches",
            definition="Times the phone's dialler was opened from the application in the period. Opening the dialler is not a call.",
            basis="recorded_by_system",
        ),
        Metric(
            key="reported_calls",
            label="Calls reported",
            value=n["reported_calls"],
            unit="count",
            drill="reported_calls",
            definition="Call attempts for which a person reported an outcome in the period. Not confirmed by a telephone provider.",
            basis="reported_by_people",
        ),
        Metric(
            key="connected_call_rate",
            label="Connected-call rate",
            value=_rate(n["connected_calls"], n["reported_calls"]),
            unit="percent",
            numerator=n["connected_calls"],
            denominator=n["reported_calls"],
            drill="connected_calls",
            definition="Reported calls in which someone was reached (connected, follow-up requested, or not interested), divided by "
            "all calls with a reported outcome. Dialler openings are not part of this.",
            basis="reported_by_people",
        ),
        Metric(
            key="follow_up_permissions",
            label="Asked to be contacted again",
            value=n["follow_up_permissions"],
            unit="count",
            drill="follow_up_permissions",
            basis="reported_by_people",
            definition="Reported calls whose outcome was a request for a follow-up. A request for one follow-up is not consent to campaigns.",
        ),
        Metric(
            key="callbacks_due",
            label="Follow-ups due",
            value=n["callbacks_due"],
            unit="count",
            drill="callbacks_due",
            definition="Follow-up tasks whose due time falls in the period, excluding cancelled ones.",
            basis="recorded_by_system",
        ),
        Metric(
            key="follow_up_completion",
            label="Follow-ups completed",
            value=_rate(n["callbacks_completed"], n["callbacks_due"]),
            unit="percent",
            numerator=n["callbacks_completed"],
            denominator=n["callbacks_due"],
            drill="callbacks_completed",
            definition="Of the follow-up tasks due in the period, those marked done (whenever that happened).",
            basis="reported_by_people",
        ),
        Metric(
            key="emails_accepted",
            label="Emails accepted by the mailbox provider",
            value=n["emails_accepted"],
            unit="count",
            drill="emails_accepted",
            definition="Messages the mailbox provider accepted in the period. Acceptance is not delivery.",
            basis="from_mailbox",
        ),
        Metric(
            key="replies",
            label="Replies received",
            value=n["replies"],
            unit="count",
            drill="replies",
            basis="from_mailbox",
            definition="Incoming messages classified as a reply (not automatic replies or bounces) in the period.",
            note="Whether a reply is positive is not recorded: nobody marks it, and it is not inferred.",
        ),
        Metric(
            key="response_time",
            label="Median time to answer a reply",
            unit="hours",
            value=round(float(response["median"]), 1) if response["median"] is not None else None,
            denominator=int(response["answered"]),
            definition="For replies received in the period that were answered: hours from the reply to the next message sent in that "
            "conversation. Replies not yet answered are not included.",
            basis="from_mailbox",
        ),
        Metric(
            key="wins",
            label="Opportunities won",
            value=n["wins"],
            unit="count",
            drill="wins",
            definition="Opportunities closed in the period in a stage marked as won.",
            basis="reported_by_people",
        ),
        Metric(
            key="research_cost_per_qualified_lead",
            label="Research cost per qualified lead",
            value=cost_per_lead,
            unit="money",
            denominator=research_leads,
            note=cost_note,
            basis="estimated_and_verified_costs",
            definition="Research costs recorded in the period (estimated and verified, one currency) divided by qualified leads that "
            "came from research and were created in the period.",
        ),
    ]
    stages = many(
        ctx,
        "SELECT st.name AS stage, st.kind, "
        "(SELECT count(*) FROM deal_stage_changes ch WHERE ch.tenant_id = st.tenant_id AND ch.to_stage_id = st.id "
        " AND ch.created_at >= :s AND ch.created_at < :e) AS entered_in_period, "
        "count(d.id) FILTER (WHERE st.kind = 'open') AS open_now, "
        "avg(EXTRACT(EPOCH FROM (now() - COALESCE(lc.at, d.created_at))) / 86400) FILTER (WHERE st.kind = 'open') AS average_days_in_stage, "
        "max(EXTRACT(EPOCH FROM (now() - COALESCE(lc.at, d.created_at))) / 86400) FILTER (WHERE st.kind = 'open') AS oldest_days_in_stage "
        "FROM pipeline_stages st LEFT JOIN deals d ON d.tenant_id = st.tenant_id AND d.stage_id = st.id "
        "LEFT JOIN LATERAL (SELECT max(ch.created_at) AS at FROM deal_stage_changes ch WHERE ch.tenant_id = d.tenant_id "
        " AND ch.deal_id = d.id AND ch.to_stage_id = d.stage_id) lc ON true "
        "WHERE st.tenant_id = :tenant_id GROUP BY st.id, st.name, st.kind, st.position, st.tenant_id ORDER BY st.position",
        w,
    )
    won = many(
        ctx,
        "SELECT COALESCE(d.currency, '---') AS currency, sum(d.amount) AS amount FROM deals d JOIN pipeline_stages st "
        "ON st.tenant_id = d.tenant_id AND st.id = d.stage_id WHERE d.tenant_id = :tenant_id AND st.kind = 'won' AND d.amount IS NOT NULL "
        "AND d.closed_at >= :s AND d.closed_at < :e GROUP BY 1 ORDER BY 1",
        w,
    )
    return FunnelOut(
        date_from=start_day,
        date_to=end_day,
        timezone=zone,
        cohort_rule=f"Each figure counts events whose own date falls on {start_day} to {end_day} inclusive, as whole days in {zone}.",
        metrics=metrics,
        stages=[
            StageRow(
                stage=r["stage"],
                kind=r["kind"],
                entered_in_period=r["entered_in_period"],
                open_now=r["open_now"],
                average_days_in_stage=round(float(r["average_days_in_stage"]), 1)
                if r["average_days_in_stage"] is not None
                else None,
                oldest_days_in_stage=round(float(r["oldest_days_in_stage"]), 1)
                if r["oldest_days_in_stage"] is not None
                else None,
            )
            for r in stages
        ],
        won_amounts=[
            MoneyLine(currency=r["currency"], amount=str(r["amount"]), basis="entered on the opportunity") for r in won
        ],
        research_costs=cost_lines,
        not_collected=[
            "Booked meetings: there is no meeting record yet, and a meeting is never inferred from the wording of a message.",
            "Proposals: counted only as opportunities entering a stage; see the stage table.",
            "Email opens and clicks: not collected. They would be approximate at best.",
            "Positive replies: not recorded.",
        ],
    )


@router.get("/reports/records", response_model=list[RecordOut], operation_id="listReportRecords", tags=["reports"])
def records(
    metric: Annotated[str, Query(max_length=60)],
    ctx: TenantContext = REPORTS,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
) -> list[RecordOut]:
    """The records behind one figure, newest first."""
    if metric not in RECORDS:
        raise ValidationFailed("There is no list for that figure.")
    _, _, start, end, _ = _window(ctx, date_from, date_to)
    rows = many(
        ctx, f"SELECT * FROM ({RECORDS[metric]}) q ORDER BY at DESC NULLS LAST LIMIT 200", {"s": start, "e": end}
    )
    return [RecordOut(**r) for r in rows]


class AttentionItem(BaseModel):
    key: str
    label: str
    count: int
    link: str
    examples: list[str]


class AttentionOut(BaseModel):
    generated_at: datetime
    items: list[AttentionItem]


@router.get("/workspace/attention", response_model=AttentionOut, operation_id="getAttention", tags=["reports"])
def attention(ctx: TenantContext = READ) -> AttentionOut:
    """What needs someone today. Each line is a count with a few examples and where to go."""
    zone = ZoneInfo(scalar(ctx, "SELECT timezone FROM tenants WHERE id = :tenant_id"))
    end_of_today = datetime.combine(utcnow().astimezone(zone).date() + timedelta(days=1), time.min, tzinfo=zone)
    queries: list[tuple[str, str, str, str]] = [
        ("replies", "Replies waiting for an answer", "/email",
         "SELECT COALESCE(th.subject, '(no subject)') AS label FROM email_threads th WHERE th.tenant_id = :tenant_id AND th.link_state = 'linked' "
         "AND (SELECT m.direction || ':' || m.classification FROM email_messages m WHERE m.tenant_id = th.tenant_id AND m.thread_id = th.id "
         "ORDER BY m.sent_at DESC LIMIT 1) = 'inbound:reply' ORDER BY th.last_message_at DESC"),
        ("unmatched", "Messages not matched to a prospect", "/email",
         "SELECT COALESCE(subject, '(no subject)') AS label FROM email_threads WHERE tenant_id = :tenant_id AND link_state IN ('unmatched', 'conflict') "
         "ORDER BY last_message_at DESC"),
        ("follow_ups", "Follow-ups due", "/tasks",
         "SELECT title AS label FROM tasks WHERE tenant_id = :tenant_id AND status = 'open' AND due_at < :eod ORDER BY due_at"),
        ("unassigned", "Qualified leads with no owner", "/prospects",
         "SELECT c.name AS label FROM leads l JOIN companies c ON c.tenant_id = l.tenant_id AND c.id = l.company_id "
         "WHERE l.tenant_id = :tenant_id AND l.status = 'qualified' AND l.owner_user_id IS NULL ORDER BY l.created_at"),
        ("stale_deals", f"Opportunities with no movement for {STALE_DEAL_DAYS} days", "/opportunities",
         "SELECT d.title AS label FROM deals d JOIN pipeline_stages st ON st.tenant_id = d.tenant_id AND st.id = d.stage_id "
         "WHERE d.tenant_id = :tenant_id AND st.kind = 'open' AND d.updated_at < now() - make_interval(days => :stale) ORDER BY d.updated_at"),
        ("verifications", "Research findings to verify", "/verification",
         "SELECT COALESCE(o.finding, 'finding') AS label FROM observations o WHERE o.tenant_id = :tenant_id AND o.verification_state = 'unverified' "
         "ORDER BY o.created_at"),
        ("drafts", "Email drafts waiting", "/email",
         "SELECT subject AS label FROM email_drafts WHERE tenant_id = :tenant_id AND status IN ('draft', 'approved') ORDER BY created_at"),
        ("sends", "Emails that did not go out cleanly", "/email",
         "SELECT to_address::text AS label FROM send_intents WHERE tenant_id = :tenant_id AND state IN ('unknown', 'failed', 'blocked') "
         "AND resolved_at IS NULL ORDER BY created_at"),
        ("mailbox", "Mailbox problems", "/integrations",
         "SELECT email_address::text || ': ' || COALESCE(last_error, status) AS label FROM mailboxes WHERE tenant_id = :tenant_id "
         "AND (status IN ('degraded', 'revoked') OR alert IS NOT NULL)"),
        ("providers", "Provider connections failing", "/integrations",
         "SELECT label || ': ' || COALESCE(last_error, status) AS label FROM provider_connections WHERE tenant_id = :tenant_id AND status = 'error'"),
        ("webhooks", "Webhook deliveries failing", "/integrations",
         "SELECT url AS label FROM webhook_endpoints WHERE tenant_id = :tenant_id AND consecutive_failures >= 3"),
        ("research", "Research runs paused", "/research",
         "SELECT COALESCE(error, 'paused') AS label FROM research_runs WHERE tenant_id = :tenant_id AND status = 'paused' ORDER BY created_at DESC"),
    ]  # fmt: skip
    items = []
    params = {"eod": end_of_today, "stale": STALE_DEAL_DAYS}
    for key, label, link, sql in queries:
        rows = many(ctx, f"SELECT label FROM ({sql}) q LIMIT 200", params)
        if rows:
            items.append(
                AttentionItem(
                    key=key, label=label, count=len(rows), link=link, examples=[str(r["label"])[:120] for r in rows[:3]]
                )
            )
    return AttentionOut(generated_at=utcnow(), items=items)
