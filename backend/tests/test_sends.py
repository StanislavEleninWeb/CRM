# ruff: noqa: F811
"""Approving and sending one email reliably. The mailbox is a local stand-in; nothing leaves the machine."""

import threading
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import RlsContext, session_scope
from app.core.time import local_to_utc
from app.modules.email import dispatch
from app.modules.email.gmail import FakeMailbox, MailboxError
from app.modules.email.unsubscribe import make_token
from app.worker import due
from tests.helpers import API, create_workspace, join, sign_in
from tests.test_email import (  # noqa: F401
    MAILBOX,
    PROSPECT,
    approve_policy,
    classify,
    connected,
    gmail,
    import_register,
    lead_with_email,
    owner,
    run_sync,
)
from tests.test_import import ok


def sql(gmail: dict[str, Any], statement: str, **params: Any) -> Any:
    with session_scope(RlsContext(tenant_id=gmail["tenant"])) as db:
        result = db.execute(text(statement), params)
        return [dict(r) for r in result.mappings()] if result.returns_rows else None


def intent_row(gmail: dict[str, Any], intent_id: str) -> dict[str, Any]:
    return sql(gmail, "SELECT * FROM send_intents WHERE id = :id", id=intent_id)[0]  # type: ignore[no-any-return]


@pytest.fixture
def ready(owner: TestClient, gmail: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Everything in place for one unsolicited message that the rules allow, with sending switched on."""
    monkeypatch.setattr(get_settings(), "email_dispatch", "live")
    lead = lead_with_email(owner)
    connected(owner, gmail)
    approve_policy(owner)
    import_register(owner, [])
    classify(owner, PROSPECT)
    sql(gmail, "UPDATE mailboxes SET min_send_interval_seconds = 0")
    draft = ok(owner.post(f"{API}/email-drafts", json={"lead_id": lead["id"]}), 201)
    assert draft["eligibility"]["outcome"] == "allow"
    return {"lead": lead, "draft": draft, **gmail}


def approve(owner: TestClient, draft_id: str, **body: Any) -> dict[str, Any]:
    return ok(owner.post(f"{API}/email-drafts/{draft_id}/approve", json=body))  # type: ignore[no-any-return]


def request_send(owner: TestClient, draft_id: str, **body: Any) -> dict[str, Any]:
    approve(owner, draft_id)
    return ok(owner.post(f"{API}/email-drafts/{draft_id}/send", json=body))  # type: ignore[no-any-return]


def run(ready: dict[str, Any], intent_id: str) -> Any:
    return dispatch.process(ready["tenant"], UUID(intent_id))


def run_due(kind: str = "email.send") -> list[str]:
    return [due.run_claimed(job) for job in due.claim_due(50) if job["kind"] == kind]


# --- the ordinary path -----------------------------------------------------------------------


def test_one_approved_email_is_sent_once_and_its_reply_is_tracked(owner: TestClient, ready: dict[str, Any]) -> None:
    box: FakeMailbox = ready["box"]
    draft = ready["draft"]
    # Not approved yet: asking to send is refused, and nothing is scheduled.
    assert owner.post(f"{API}/email-drafts/{draft['id']}/send", json={}).status_code == 409
    approved = approve(owner, draft["id"])
    assert approved["status"] == "approved" and approved["approved_version"] == 1
    intent = ok(owner.post(f"{API}/email-drafts/{draft['id']}/send", json={}))
    assert intent["state"] == "queued" and intent["dry_run"] is False and intent["delivery_evidence"] == "none"
    assert ok(owner.get(f"{API}/email-drafts/{draft['id']}"))["status"] == "queued"
    # Asking again, or twice at once from two tabs, returns the same request.
    assert ok(owner.post(f"{API}/email-drafts/{draft['id']}/send", json={}))["id"] == intent["id"]
    assert box.sent == []  # the request itself sends nothing

    assert run_due() == ["done"]
    sent = intent_row(ready, intent["id"])
    assert sent["state"] == "provider_accepted" and sent["provider_message_id"] and sent["accepted_at"] is not None
    assert len(box.sent) == 1
    message = message_from_bytes(box.sent[0])
    assert message["To"] == PROSPECT and MAILBOX in message["From"] and message["Message-ID"] == sent["rfc_message_id"]
    assert "/api/v1/unsubscribe/" in " ".join(message["List-Unsubscribe"].split())
    assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    body = message.get_payload(decode=True).decode()  # type: ignore[union-attr]
    assert (
        "Непоискано търговско съобщение" in body
        and "SEWEB Ltd, Sofia" in body
        and body.strip() == draft["eligibility"]["rendered_body"].strip()
    )

    after = ok(owner.get(f"{API}/email-drafts/{draft['id']}"))
    assert after["status"] == "sent" and after["send"]["state"] == "provider_accepted"
    assert owner.patch(f"{API}/email-drafts/{draft['id']}", json={"subject": "Changed"}).status_code == 409
    assert ok(owner.get(f"{API}/leads/{ready['lead']['id']}"))["outreach_status"] == "emailed"
    kinds = [a["kind"] for a in ok(owner.get(f"{API}/activities", params={"lead_id": ready["lead"]["id"]}))["items"]]
    assert kinds.count("email.sent") == 1
    events = [e["event_type"] for e in sql(ready, "SELECT event_type FROM outbox_events ORDER BY created_at")]
    assert events == ["email.send.queued", "email.send.accepted"]
    stages = [
        d["stage"]
        for d in sql(ready, "SELECT stage FROM eligibility_decisions WHERE outcome = 'allow' ORDER BY created_at")
    ]
    assert stages == ["request", "request", "dispatch"]  # approval, send request, and again just before sending

    # Running the same work again changes nothing.
    assert run(ready, intent["id"]) is None and len(box.sent) == 1
    # The sent message appears in the conversation, and a reply is evidence it arrived.
    run_sync(ready)
    thread = ok(owner.get(f"{API}/email-threads", params={"lead_id": ready["lead"]["id"]}))["items"][0]
    assert [m["direction"] for m in thread["messages"]] == ["outbound"]
    box.add(
        sender=PROSPECT,
        subject="Re: Salon Aurora",
        thread_id=sent["provider_thread_id"],
        headers={"In-Reply-To": sent["rfc_message_id"]},
    )
    run_sync(ready)
    assert intent_row(ready, intent["id"])["delivery_evidence"] == "replied"
    assert ok(owner.get(f"{API}/leads/{ready['lead']['id']}"))["outreach_status"] == "replied"


def test_sending_is_off_by_default_and_a_dry_run_sends_nothing(
    owner: TestClient, ready: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    box: FakeMailbox = ready["box"]
    draft = ready["draft"]
    assert type(get_settings()).model_fields["email_dispatch"].default == "off"
    monkeypatch.setattr(get_settings(), "email_dispatch", "off")
    approve(owner, draft["id"])
    refused = owner.post(f"{API}/email-drafts/{draft['id']}/send", json={})
    assert refused.status_code == 409 and "switched off" in refused.json()["error"]["message"]
    assert ok(owner.get(f"{API}/email-sending"))["mode"] == "off"

    monkeypatch.setattr(get_settings(), "email_dispatch", "dry_run")
    intent = ok(owner.post(f"{API}/email-drafts/{draft['id']}/send", json={}))
    assert intent["dry_run"] is True
    # Even if the switch is turned to live afterwards, a request made as a dry run stays one.
    monkeypatch.setattr(get_settings(), "email_dispatch", "live")
    assert run(ready, intent["id"]) is None
    row = intent_row(ready, intent["id"])
    assert (
        row["state"] == "simulated"
        and "nothing was sent" in row["state_reason"]
        and box.sent == []
        and "send" not in box.calls
    )
    after = ok(owner.get(f"{API}/email-drafts/{draft['id']}"))
    assert after["status"] == "draft" and after["version"] == 2 and after["approved_version"] is None

    # A request made while live is not sent if sending has been switched off by the time it is due.
    live = request_send(owner, draft["id"])
    monkeypatch.setattr(get_settings(), "email_dispatch", "off")
    assert run(ready, live["id"]) is None
    assert intent_row(ready, live["id"])["state"] == "blocked" and box.sent == []


# --- approval is bound to what was approved --------------------------------------------------


def test_any_change_after_approval_needs_a_new_approval(owner: TestClient, ready: dict[str, Any]) -> None:
    box: FakeMailbox = ready["box"]
    draft = ready["draft"]
    approve(owner, draft["id"])
    edited = ok(owner.patch(f"{API}/email-drafts/{draft['id']}", json={"body_text": "A different offer entirely."}))
    assert edited["status"] == "draft" and edited["approved_version"] is None and edited["version"] == 2
    assert owner.post(f"{API}/email-drafts/{draft['id']}/send", json={}).status_code == 409
    for change in ({"subject": "New subject"}, {"to_address": "other@example-salon.bg"}):
        approve(owner, draft["id"])
        assert ok(owner.patch(f"{API}/email-drafts/{draft['id']}", json=change))["status"] == "draft"
    ok(owner.patch(f"{API}/email-drafts/{draft['id']}", json={"to_address": PROSPECT}))

    # The sender's identification is part of what was approved.
    approve(owner, draft["id"])
    ok(owner.put(f"{API}/outreach-policy/sender-identity", json={"sender_identity": "SEWEB EOOD, Plovdiv, Bulgaria"}))
    changed = owner.post(f"{API}/email-drafts/{draft['id']}/send", json={})
    assert changed.status_code == 409 and "changed after approval" in changed.json()["error"]["message"]

    # The same change arriving after the request, before it is due, stops the message at the last check.
    intent = request_send(owner, draft["id"], scheduled_local=_local(timedelta(hours=2)))
    ok(
        owner.put(
            f"{API}/outreach-policy/sender-identity",
            json={"sender_identity": "SEWEB Ltd, Sofia, Bulgaria, office@seweb.example"},
        )
    )
    sql(ready, "UPDATE send_intents SET scheduled_for = now()")
    assert run(ready, intent["id"]) is None
    row = intent_row(ready, intent["id"])
    assert row["state"] == "blocked" and "changed after approval" in row["state_reason"] and box.sent == []
    assert ok(owner.get(f"{API}/email-drafts/{draft['id']}"))["status"] == "draft"  # editable again, approval gone
    assert ok(owner.get(f"{API}/send-intents", params={"needs_attention": True}))[0]["id"] == intent["id"]


def test_a_recipient_needing_review_is_sent_only_with_a_recorded_review(
    owner: TestClient, ready: dict[str, Any]
) -> None:
    box: FakeMailbox = ready["box"]
    classify(owner, PROSPECT, "sole_trader", "business")
    draft = ready["draft"]
    refused = owner.post(f"{API}/email-drafts/{draft['id']}/approve", json={})
    assert refused.status_code == 422 and "Say what you checked" in refused.json()["error"]["message"]
    approved = approve(
        owner, draft["id"], review_note="Trades under a registered business name; address is on their price list"
    )
    assert approved["status"] == "approved" and approved["review_note"].startswith("Trades under")
    intent = ok(
        owner.post(f"{API}/email-drafts/{draft['id']}/send", json={"scheduled_local": _local(timedelta(hours=1))})
    )
    # A reason nobody reviewed appears before it is due: it is not covered by the earlier review.
    sql(ready, "DELETE FROM recipient_profiles")
    sql(ready, "UPDATE send_intents SET scheduled_for = now()")
    run(ready, intent["id"])
    assert intent_row(ready, intent["id"])["state"] == "blocked" and box.sent == []

    # A block can never be approved, whatever note is given.
    ok(owner.post(f"{API}/email-suppressions", json={"value": PROSPECT}), 201)
    blocked = owner.post(
        f"{API}/email-drafts/{draft['id']}/approve", json={"review_note": "I insist that this is fine"}
    )
    assert blocked.status_code == 409 and "may not be sent" in blocked.json()["error"]["message"]
    refusals = sql(
        ready, "SELECT count(*) AS n FROM eligibility_decisions WHERE outcome = 'block' AND stage = 'request'"
    )
    assert refusals[0]["n"] == 1  # the refused attempt is on record


# --- things that happen between the request and the send -------------------------------------


def _local(ahead: timedelta) -> str:
    return (datetime.now(ZoneInfo("Europe/Sofia")) + ahead).strftime("%Y-%m-%dT%H:%M")


def test_an_opt_out_reply_or_stale_register_before_dispatch_stops_the_message(
    owner: TestClient, ready: dict[str, Any], client: TestClient, migrator_engine: Any
) -> None:
    box: FakeMailbox = ready["box"]
    draft = ready["draft"]

    # 1. The recipient opts out through the link while the message waits.
    intent = request_send(owner, draft["id"], scheduled_local=_local(timedelta(hours=3)))
    assert client.post(f"{API}/unsubscribe/{make_token(ready['tenant'], PROSPECT)}").status_code == 200
    row = intent_row(ready, intent["id"])
    assert row["state"] == "cancelled" and "do-not-email" in row["state_reason"]
    assert ok(owner.get(f"{API}/email-drafts/{draft['id']}"))["status"] == "draft"
    sql(ready, "UPDATE due_jobs SET due_at = now() WHERE kind = 'email.send'")
    assert run_due() == ["done"] and box.sent == []
    assert owner.post(f"{API}/email-drafts/{draft['id']}/approve", json={}).status_code == 409  # still suppressed
    suppression = ok(owner.get(f"{API}/email-suppressions"))["items"][0]
    ok(
        owner.post(
            f"{API}/email-suppressions/{suppression['id']}/lift",
            json={"lift_note": "Asked by phone to be contacted again"},
        )
    )

    # 2. The register on file goes out of date while the message waits.
    intent = request_send(owner, draft["id"], scheduled_local=_local(timedelta(hours=3)))
    with migrator_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(ready["tenant"])})
        conn.execute(text("UPDATE regulatory_sources SET obtained_at = now() - interval '8 days'"))
    sql(ready, "UPDATE send_intents SET scheduled_for = now() WHERE id = :id", id=intent["id"])
    run(ready, intent["id"])
    row = intent_row(ready, intent["id"])
    assert row["state"] == "blocked" and "register" in row["state_reason"].lower() and box.sent == []
    import_register(owner, [])

    # 3. They write to us first. The cold message is stopped; a reply we are writing to them is not.
    cold = request_send(owner, draft["id"], scheduled_local=_local(timedelta(hours=3)))
    box.add(sender=PROSPECT, subject="Do you do online booking?", headers={"Message-ID": "<q1@example-salon.bg>"})
    run_sync(ready)
    assert (
        intent_row(ready, cold["id"])["state"] == "cancelled"
        and "replied" in intent_row(ready, cold["id"])["state_reason"]
    )
    thread = ok(owner.get(f"{API}/email-threads", params={"lead_id": ready["lead"]["id"]}))["items"][0]
    reply = ok(
        owner.post(
            f"{API}/email-drafts", json={"kind": "reply", "thread_id": thread["id"], "body_text": "Yes, we do."}
        ),
        201,
    )
    reply_intent = request_send(owner, reply["id"], scheduled_local=_local(timedelta(hours=3)))
    box.add(sender=PROSPECT, subject="One more question", thread_id=next(iter(box.messages.values())).thread_id)
    run_sync(ready)
    assert intent_row(ready, reply_intent["id"])["state"] == "queued"  # an ordinary conversation is not disabled
    sql(ready, "UPDATE send_intents SET scheduled_for = now() WHERE id = :id", id=reply_intent["id"])
    run(ready, reply_intent["id"])
    assert intent_row(ready, reply_intent["id"])["state"] == "provider_accepted" and len(box.sent) == 1
    answer = message_from_bytes(box.sent[0])
    assert answer["List-Unsubscribe"] is None and "Непоискано" not in answer.get_payload(decode=True).decode()  # type: ignore[union-attr]
    assert answer["In-Reply-To"] and answer["To"] == PROSPECT


def test_a_waiting_message_can_be_cancelled_but_a_sent_one_cannot(owner: TestClient, ready: dict[str, Any]) -> None:
    box: FakeMailbox = ready["box"]
    draft = ready["draft"]
    intent = request_send(owner, draft["id"], scheduled_local=_local(timedelta(days=2)))
    assert run_due() == []  # not due: nothing is claimed, and nothing sits in the task queue waiting
    waiting = ok(owner.get(f"{API}/email-sending"))
    assert (waiting["waiting"], waiting["needs_attention"], waiting["sent_last_24h"], waiting["daily_limit"]) == (
        1,
        0,
        0,
        50,
    )
    cancelled = ok(owner.post(f"{API}/send-intents/{intent['id']}/cancel"))
    assert cancelled["state"] == "cancelled"
    assert owner.post(f"{API}/send-intents/{intent['id']}/cancel").status_code == 409
    sql(ready, "UPDATE due_jobs SET due_at = now() WHERE kind = 'email.send'")
    assert run_due() == ["done"] and box.sent == []

    sent = request_send(owner, draft["id"])
    run(ready, sent["id"])
    refused = owner.post(f"{API}/send-intents/{sent['id']}/cancel")
    assert refused.status_code == 409 and "cannot be called back" in refused.json()["error"]["message"]
    assert len(box.sent) == 1


# --- exactly once ----------------------------------------------------------------------------


def test_many_workers_and_duplicate_tasks_send_one_message(owner: TestClient, ready: dict[str, Any]) -> None:
    box: FakeMailbox = ready["box"]
    intent = request_send(owner, ready["draft"]["id"])
    jobs = [job for job in due.claim_due(50) if job["kind"] == "email.send"]
    assert len(jobs) == 1 and [j for j in due.claim_due(50) if j["kind"] == "email.send"] == []  # one poller gets it
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(n: int) -> None:
        try:
            barrier.wait()
            # Half arrive as the same task delivered again, half as stray direct calls.
            due.run_claimed(jobs[0]) if n % 2 else dispatch.process(ready["tenant"], UUID(intent["id"]))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(box.sent) == 1 and box.calls.count("send") == 1
    assert intent_row(ready, intent["id"])["state"] == "provider_accepted"
    assert sql(ready, "SELECT count(*) AS n FROM activities WHERE kind = 'email.sent'")[0]["n"] == 1


def test_no_database_lock_or_open_transaction_spans_the_provider_call(
    owner: TestClient, ready: dict[str, Any], migrator_engine: Any
) -> None:
    box: FakeMailbox = ready["box"]
    intent = request_send(owner, ready["draft"]["id"])
    job = next(j for j in due.claim_due(50) if j["kind"] == "email.send")
    in_flight, release = threading.Event(), threading.Event()
    real_send = box.send

    def slow_send(token: str, raw: bytes, thread_id: str | None) -> tuple[str, str]:
        in_flight.set()
        assert release.wait(timeout=20)
        return real_send(token, raw, thread_id)

    box.send = slow_send  # type: ignore[method-assign]
    result: list[str] = []
    worker = threading.Thread(target=lambda: result.append(due.run_claimed(job)))
    worker.start()
    try:
        assert in_flight.wait(timeout=20)
        with migrator_engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(ready["tenant"])})
            # Every row the send touches can be locked by someone else right now.
            for table, key in (
                ("send_intents", intent["id"]),
                ("email_drafts", ready["draft"]["id"]),
                ("due_jobs", str(job["id"])),
            ):
                assert (
                    conn.execute(text(f"SELECT 1 FROM {table} WHERE id = :id FOR UPDATE NOWAIT"), {"id": key}).scalar()
                    == 1
                )
            assert conn.execute(text("SELECT 1 FROM mailboxes FOR UPDATE NOWAIT")).scalar() == 1
            conn.rollback()
            conn.execute(text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(ready["tenant"])})
            open_transactions = conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() "
                    "AND state LIKE 'idle in transaction%'"
                )
            ).scalar()
            assert open_transactions == 0
            assert (
                conn.execute(text("SELECT state FROM send_intents WHERE id = :id"), {"id": intent["id"]}).scalar()
                == "dispatching"
            )
            # An opt-out arriving now is recorded, but this message is already past the point of no return.
        ok(owner.post(f"{API}/email-suppressions", json={"value": PROSPECT, "reason": "opt_out"}), 201)
    finally:
        release.set()
        worker.join(timeout=20)
    assert result == ["done"] and len(box.sent) == 1
    assert intent_row(ready, intent["id"])["state"] == "provider_accepted"


# --- uncertain outcomes are reconciled, never resent -----------------------------------------


def test_a_timeout_after_which_the_message_did_go_is_found_and_not_sent_again(
    owner: TestClient, ready: dict[str, Any]
) -> None:
    box: FakeMailbox = ready["box"]
    intent = request_send(owner, ready["draft"]["id"])
    box.fail["send"] = [MailboxError("The mailbox provider timed out.", ambiguous=True)]
    box.send_lands_despite_error = True
    assert run_due() == ["rescheduled"]
    row = intent_row(ready, intent["id"])
    assert row["state"] == "unknown" and "may have been sent" in row["state_reason"]
    assert (
        ok(owner.get(f"{API}/email-drafts/{ready['draft']['id']}"))["status"] == "queued"
    )  # not editable while unknown
    check_at = sql(ready, "SELECT due_at FROM due_jobs WHERE kind = 'email.send'")[0]["due_at"]
    assert timedelta(seconds=50) < check_at - datetime.now(UTC) <= timedelta(seconds=60)  # the wait is in the database

    sql(ready, "UPDATE due_jobs SET due_at = now() WHERE kind = 'email.send'")
    assert run_due() == ["done"]
    row = intent_row(ready, intent["id"])
    assert row["state"] == "provider_accepted" and "Found in the mailbox" in row["state_reason"]
    assert box.calls.count("send") == 1 and len(box.sent) == 1  # checked, not resent
    assert ok(owner.get(f"{API}/email-drafts/{ready['draft']['id']}"))["status"] == "sent"


def test_an_unresolved_send_waits_for_a_person_and_is_never_retried(
    owner: TestClient, ready: dict[str, Any], make_client: Any
) -> None:
    box: FakeMailbox = ready["box"]
    draft = ready["draft"]
    intent = request_send(owner, draft["id"])
    box.fail["send"] = [MailboxError("The mailbox provider timed out.", ambiguous=True)]  # and it did not land
    assert run_due() == ["rescheduled"]
    waits = []
    for _ in range(3):
        before = datetime.now(UTC)
        sql(ready, "UPDATE due_jobs SET due_at = now() WHERE kind = 'email.send'")
        outcome = run_due()
        after_due = sql(ready, "SELECT due_at, status FROM due_jobs WHERE kind = 'email.send'")[0]
        waits.append(
            (
                outcome,
                round((after_due["due_at"] - before).total_seconds() / 60)
                if after_due["status"] == "pending"
                else None,
            )
        )
    assert waits == [(["rescheduled"], 5), (["rescheduled"], 15), (["done"], None)]  # backs off, then stops checking
    row = intent_row(ready, intent["id"])
    assert (
        row["state"] == "unknown" and row["reconcile_attempts"] == 3 and "record what happened" in row["state_reason"]
    )
    assert box.calls.count("send") == 1 and box.calls.count("find_by_rfc_id") == 3 and box.sent == []
    # Nothing brings it back by itself: not the worker, not a new request for the same version.
    assert run(ready, intent["id"]) is None and box.calls.count("send") == 1
    assert ok(owner.post(f"{API}/email-drafts/{draft['id']}/send", json={}))["id"] == intent["id"]
    listed = ok(owner.get(f"{API}/send-intents", params={"needs_attention": True}))
    assert [(i["id"], i["state"], i["subject"]) for i in listed] == [(intent["id"], "unknown", draft["subject"])]
    assert ok(owner.get(f"{API}/email-sending"))["needs_attention"] == 1

    rep = make_client()
    sign_in(rep, "rep@example.test")
    join(owner, rep, "rep@example.test", "representative")
    assert (
        rep.post(
            f"{API}/send-intents/{intent['id']}/resolve", json={"sent": False, "note": "Not in the Sent folder"}
        ).status_code
        == 403
    )
    assert (
        owner.post(f"{API}/send-intents/{intent['id']}/resolve", json={"sent": False, "note": "no"}).status_code == 422
    )
    resolved = ok(
        owner.post(
            f"{API}/send-intents/{intent['id']}/resolve",
            json={"sent": False, "note": "Checked the Sent folder: it is not there"},
        )
    )
    assert resolved["state"] == "failed"
    again = ok(owner.get(f"{API}/email-drafts/{draft['id']}"))
    assert again["status"] == "draft" and again["version"] == 2 and again["approved_version"] is None
    assert (
        owner.post(
            f"{API}/send-intents/{intent['id']}/resolve", json={"sent": True, "note": "Changed my mind about it"}
        ).status_code
        == 409
    )
    # Sending it now is a new, separately approved request.
    second = request_send(owner, draft["id"])
    assert second["id"] != intent["id"] and run(ready, second["id"]) is None and len(box.sent) == 1
    assert "email.send_resolved" in [e["action"] for e in ok(owner.get(f"{API}/audit-events"))["items"]]

    # The other answer: a person found it in the mailbox.
    third_draft = ok(
        owner.post(
            f"{API}/email-drafts", json={"lead_id": ready["lead"]["id"], "subject": "Second note", "body_text": "x"}
        ),
        201,
    )
    third = request_send(owner, third_draft["id"])
    box.fail["send"] = [MailboxError("The mailbox provider could not be reached.", ambiguous=True)]
    run(ready, third["id"])
    confirmed = ok(
        owner.post(
            f"{API}/send-intents/{third['id']}/resolve", json={"sent": True, "note": "It is in the Sent folder, 10:42"}
        )
    )
    assert (
        confirmed["state"] == "provider_accepted"
        and ok(owner.get(f"{API}/email-drafts/{third_draft['id']}"))["status"] == "sent"
    )


def test_a_worker_that_dies_is_recovered_without_a_second_send(owner: TestClient, ready: dict[str, Any]) -> None:
    box: FakeMailbox = ready["box"]
    tenant = ready["tenant"]
    # Dies after claiming, before the final check: nothing was sent, so another worker may take over.
    first = request_send(owner, ready["draft"]["id"])
    step, old_lease = dispatch._claim(tenant, UUID(first["id"]))
    assert step == "go" and intent_row(ready, first["id"])["state"] == "claimed"
    wait = run(ready, first["id"])
    assert isinstance(wait, datetime) and box.sent == []  # the lease is respected while it is live
    sql(ready, "UPDATE send_intents SET lease_expires_at = now() - interval '1 second'")
    assert run(ready, first["id"]) is None and len(box.sent) == 1
    assert intent_row(ready, first["id"])["state"] == "provider_accepted"
    # The first worker wakes up late. Its lease is no longer valid, so it can neither send nor overwrite the result.
    assert dispatch._final_check(tenant, UUID(first["id"]), old_lease) is None
    assert (
        dispatch._record(
            tenant, UUID(first["id"]), old_lease, {"mailbox": {"id": uuid4()}}, {"result": "failed", "reason": "x"}
        )
        is None
    )
    assert intent_row(ready, first["id"])["state"] == "provider_accepted" and len(box.sent) == 1

    # Dies after the point of no return, with the provider call's fate unknown.
    draft = ok(
        owner.post(
            f"{API}/email-drafts", json={"lead_id": ready["lead"]["id"], "subject": "Another", "body_text": "y"}
        ),
        201,
    )
    second = request_send(owner, draft["id"])
    _, lease = dispatch._claim(tenant, UUID(second["id"]))
    prepared = dispatch._final_check(tenant, UUID(second["id"]), lease)
    assert isinstance(prepared, dict) and intent_row(ready, second["id"])["state"] == "dispatching"
    assert isinstance(run(ready, second["id"]), datetime) and len(box.sent) == 1  # still leased: left alone
    sql(ready, "UPDATE send_intents SET lease_expires_at = now() - interval '1 second' WHERE id = :id", id=second["id"])
    again_at = run(ready, second["id"])
    row = intent_row(ready, second["id"])
    assert (
        row["state"] == "unknown" and "stopped while sending" in row["state_reason"] and isinstance(again_at, datetime)
    )
    assert len(box.sent) == 1 and box.calls.count("send") == 1  # recovery looked in the mailbox; it did not send
    assert box.calls.count("find_by_rfc_id") == 1
    # In fact the dead worker's call had reached the provider. The late result is still recorded as the truth.
    assert (
        dispatch._record(
            tenant,
            UUID(second["id"]),
            lease,
            prepared,
            {"result": "accepted", "message_id": "m-late", "thread_id": "t-late"},
        )
        is None
    )
    assert intent_row(ready, second["id"])["state"] == "provider_accepted"


# --- limits, provider refusals, scheduling ---------------------------------------------------


def test_sending_limits_delay_messages_in_the_database(owner: TestClient, ready: dict[str, Any]) -> None:
    box: FakeMailbox = ready["box"]
    sql(ready, "UPDATE mailboxes SET daily_send_limit = 1, min_send_interval_seconds = 0")
    first = request_send(owner, ready["draft"]["id"])
    second_draft = ok(
        owner.post(f"{API}/email-drafts", json={"lead_id": ready["lead"]["id"], "subject": "Second", "body_text": "z"}),
        201,
    )
    second = request_send(owner, second_draft["id"])
    run(ready, first["id"])
    later = run(ready, second["id"])
    row = intent_row(ready, second["id"])
    assert row["state"] == "queued" and "daily sending limit" in row["state_reason"] and len(box.sent) == 1
    assert isinstance(later, datetime) and timedelta(hours=23, minutes=59) < later - datetime.now(UTC) <= timedelta(
        hours=24
    )
    assert row["scheduled_for"] == later

    # Spacing between messages, once the daily limit allows it.
    sql(ready, "UPDATE mailboxes SET daily_send_limit = 50, min_send_interval_seconds = 600")
    sql(ready, "UPDATE send_intents SET scheduled_for = now() WHERE id = :id", id=second["id"])
    spaced = run(ready, second["id"])
    assert isinstance(spaced, datetime) and timedelta(minutes=9) < spaced - datetime.now(UTC) <= timedelta(minutes=10)
    assert "spaced out" in intent_row(ready, second["id"])["state_reason"] and len(box.sent) == 1

    # The provider itself says "slow down": a definite refusal, so it is tried again later, a bounded number of times.
    sql(ready, "UPDATE mailboxes SET min_send_interval_seconds = 0")
    sql(ready, "UPDATE send_intents SET scheduled_for = now() WHERE id = :id", id=second["id"])
    box.fail["send"] = [
        MailboxError("The mailbox provider is rate limiting requests.", rate_limited=True, retry_after=120)
    ]
    retry = run(ready, second["id"])
    assert isinstance(retry, datetime) and timedelta(seconds=110) < retry - datetime.now(UTC) <= timedelta(seconds=120)
    assert (
        intent_row(ready, second["id"])["state"] == "queued"
        and intent_row(ready, second["id"])["dispatched_at"] is None
    )
    sql(
        ready,
        "UPDATE send_intents SET scheduled_for = now(), attempts = :n WHERE id = :id",
        id=second["id"],
        n=dispatch.MAX_PROVIDER_RETRIES,
    )
    box.fail["send"] = [MailboxError("The mailbox provider is rate limiting requests.", rate_limited=True)]
    assert run(ready, second["id"]) is None and intent_row(ready, second["id"])["state"] == "failed"
    assert len(box.sent) == 1


def test_a_refused_or_disconnected_mailbox_fails_plainly(owner: TestClient, ready: dict[str, Any]) -> None:
    box: FakeMailbox = ready["box"]
    draft = ready["draft"]
    intent = request_send(owner, draft["id"])
    box.fail["send"] = [MailboxError("The mailbox authorisation was withdrawn or expired.", revoked=True)]
    assert run(ready, intent["id"]) is None
    row = intent_row(ready, intent["id"])
    assert row["state"] == "failed" and "withdrawn" in row["state_reason"] and box.sent == []
    assert ok(owner.get(f"{API}/mailboxes"))[0]["status"] == "revoked"
    after = ok(owner.get(f"{API}/email-drafts/{draft['id']}"))
    assert after["status"] == "draft" and "no_mailbox" in [r["code"] for r in after["eligibility"]["reasons"]]
    assert owner.post(f"{API}/email-drafts/{draft['id']}/approve", json={}).status_code == 409
    # A failure is acknowledged, not converted into "sent".
    assert (
        owner.post(
            f"{API}/send-intents/{intent['id']}/resolve", json={"sent": True, "note": "Pretend that it went"}
        ).status_code
        == 409
    )
    ok(
        owner.post(
            f"{API}/send-intents/{intent['id']}/resolve",
            json={"sent": False, "note": "Seen; will reconnect the mailbox"},
        )
    )
    assert ok(owner.get(f"{API}/send-intents", params={"needs_attention": True})) == []


def test_a_chosen_time_is_wall_clock_time_in_the_workspace_zone(owner: TestClient, ready: dict[str, Any]) -> None:
    # Sofia leaves summer time on 25 October 2026 at 04:00, which becomes 03:00.
    assert local_to_utc(datetime(2026, 10, 24, 9, 0), "Europe/Sofia") == datetime(2026, 10, 24, 6, 0, tzinfo=UTC)
    assert local_to_utc(datetime(2026, 10, 26, 9, 0), "Europe/Sofia") == datetime(2026, 10, 26, 7, 0, tzinfo=UTC)
    assert local_to_utc(datetime(2026, 10, 25, 3, 30), "Europe/Sofia") == datetime(
        2026, 10, 25, 0, 30, tzinfo=UTC
    )  # the first 03:30
    with pytest.raises(ValueError, match="does not exist"):
        local_to_utc(datetime(2027, 3, 28, 3, 30), "Europe/Sofia")  # clocks jump from 03:00 to 04:00
    with pytest.raises(ValueError, match="without an offset"):
        local_to_utc(datetime(2026, 10, 24, 9, 0, tzinfo=UTC), "Europe/Sofia")

    draft = ready["draft"]
    approve(owner, draft["id"])
    for bad, message in (("2020-01-01T09:00", "in the future"), (_local(timedelta(days=90)), "at most 60 days")):
        refused = owner.post(f"{API}/email-drafts/{draft['id']}/send", json={"scheduled_local": bad})
        assert refused.status_code == 422 and message in refused.json()["error"]["message"]
    assert (
        owner.post(f"{API}/email-drafts/{draft['id']}/send", json={"scheduled_local": "tomorrow morning"}).status_code
        == 422
    )
    target = (datetime.now(ZoneInfo("Europe/Sofia")) + timedelta(days=20)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    intent = ok(
        owner.post(
            f"{API}/email-drafts/{draft['id']}/send", json={"scheduled_local": target.strftime("%Y-%m-%dT%H:%M")}
        )
    )
    stored = datetime.fromisoformat(intent["scheduled_for"])
    assert (
        stored == target.astimezone(UTC) and stored.astimezone(ZoneInfo("Europe/Sofia")).hour == 9
    )  # 09:00 there, whatever the offset then
    job = sql(ready, "SELECT due_at, status FROM due_jobs WHERE kind = 'email.send'")[0]
    assert job["due_at"] == stored and job["status"] == "pending"
    assert run_due() == [] and ready["box"].sent == []
    # Run early by mistake, it still waits for its time.
    assert run(ready, intent["id"]) == stored and ready["box"].sent == []


# --- who may do what -------------------------------------------------------------------------


def test_roles_and_tenants_are_enforced_for_sending(owner: TestClient, ready: dict[str, Any], make_client: Any) -> None:
    draft = ready["draft"]
    rep, viewer, other = make_client(), make_client(), make_client()
    sign_in(rep, "rep@example.test")
    join(owner, rep, "rep@example.test", "representative")
    sign_in(viewer, "viewer@example.test")
    join(owner, viewer, "viewer@example.test", "read_only")
    sign_in(other, "other@example.test")
    create_workspace(other, "Another customer")

    assert (
        rep.post(f"{API}/email-drafts/{draft['id']}/approve", json={}).status_code == 403
    )  # a representative cannot approve
    assert viewer.post(f"{API}/email-drafts/{draft['id']}/send", json={}).status_code == 403
    approve(owner, draft["id"])
    intent = ok(
        rep.post(f"{API}/email-drafts/{draft['id']}/send", json={"scheduled_local": _local(timedelta(hours=5))})
    )
    assert viewer.post(f"{API}/send-intents/{intent['id']}/cancel").status_code == 403
    assert ok(viewer.get(f"{API}/send-intents"))[0]["id"] == intent["id"]

    assert ok(other.get(f"{API}/send-intents")) == [] and ok(other.get(f"{API}/email-sending"))["waiting"] == 0
    for path, body in (
        (f"/email-drafts/{draft['id']}/approve", {}),
        (f"/email-drafts/{draft['id']}/send", {}),
        (f"/send-intents/{intent['id']}/cancel", None),
        (f"/send-intents/{intent['id']}/resolve", {"sent": True, "note": "Not mine to decide"}),
    ):
        assert other.post(f"{API}{path}", json=body).status_code == 404, path
    assert intent_row(ready, intent["id"])["state"] == "queued"
    other_tenant = UUID(ok(other.get(f"{API}/tenant"))["id"])
    # A worker acting for another tenant cannot see, and so cannot send, this message.
    assert dispatch.process(other_tenant, UUID(intent["id"])) is None and ready["box"].sent == []
