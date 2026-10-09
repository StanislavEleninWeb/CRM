"""Email eligibility, Gmail connection, synchronisation and conversations. No real mailbox is contacted."""

import base64
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import RlsContext, session_scope
from app.core.deps import get_redis
from app.modules.email import mime, oauth, sync
from app.modules.email.gmail import FakeMailbox, GmailProvider, HistoryExpired, MailboxError
from app.modules.email.oauth import GoogleAuth
from app.modules.email.unsubscribe import make_token, read_token
from app.worker import due
from tests.helpers import API, create_workspace, join, sign_in
from tests.test_import import import_and_commit, lead_row, ok, workbook_bytes

DOMAIN, MAILBOX = "seweb.example", "sales@seweb.example"
CLIENT_ID = "internal-client-id.apps.example"
PROSPECT = "office@example-salon.bg"


class Google:
    """Signs tokens the way Google would, with a key the test controls."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update({"kid": "g1", "use": "sig", "alg": "RS256"})
        self.jwks = {"keys": [jwk]}
        self.token_response: dict[str, Any] = {}
        self.refresh_status = 200
        self.revoked: list[str] = []
        self.revoke_status = 200

    def id_token(self, **overrides: Any) -> str:
        now = int(time.time())
        claims = {
            "iss": "https://accounts.google.com",
            "aud": CLIENT_ID,
            "sub": "google-user-1",
            "iat": now,
            "exp": now + 300,
            "email": MAILBOX,
            "email_verified": True,
            "hd": DOMAIN,
            "nonce": "set-by-test",
            **overrides,
        }
        return jwt.encode(
            {k: v for k, v in claims.items() if v is not None}, self.key, algorithm="RS256", headers={"kid": "g1"}
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/certs"):
            return httpx.Response(200, json=self.jwks)
        if request.url.path.endswith("/revoke"):
            self.revoked.append(request.content.decode())
            return httpx.Response(self.revoke_status)
        if request.url.path.endswith("/token"):
            form = dict(p.split("=", 1) for p in request.content.decode().split("&"))
            if form.get("grant_type") == "refresh_token":
                if self.refresh_status != 200:
                    return httpx.Response(self.refresh_status, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": "access-token-1"})
            return httpx.Response(200, json=self.token_response)
        return httpx.Response(404)


class Tokens(sync.TokenSource):
    def access_token(self, tenant_id: UUID, mailbox: Any) -> str:
        return "test-access-token"


@pytest.fixture
def owner(make_client: Any) -> TestClient:
    client = make_client()
    sign_in(client, "owner@example.test")
    create_workspace(client, "SEWEB")
    return client


@pytest.fixture
def gmail(owner: TestClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Gmail configured for this tenant only, with Google and the mailbox replaced by local stand-ins."""
    tenant = ok(owner.get(f"{API}/tenant"))["id"]
    settings = get_settings()
    for name, value in (
        ("gmail_client_id", CLIENT_ID),
        ("gmail_client_secret", "internal-secret"),
        ("gmail_internal_domain", DOMAIN),
        ("gmail_internal_tenant_ids", tenant),
        ("gmail_pubsub_topic", "projects/p/topics/gmail"),
        ("gmail_push_audience", "https://crm.example/api/v1/webhooks/gmail"),
        ("gmail_push_service_account", "gmail-push@p.iam.gserviceaccount.example"),
    ):
        monkeypatch.setattr(settings, name, value)
    google = Google()
    oauth.set_google_auth(GoogleAuth(settings, httpx.Client(transport=httpx.MockTransport(google.handler))))
    box = FakeMailbox(MAILBOX)
    sync.set_provider(box, Tokens())
    yield {"google": google, "box": box, "tenant": UUID(tenant)}
    oauth.set_google_auth(None)
    sync.set_provider(None, sync.TokenSource())


def connect(client: TestClient, gmail: dict[str, Any], **claims: Any) -> Any:
    """Walk the OAuth flow: start, then return from Google with a signed identity token."""
    started = client.get(f"{API}/mailboxes/gmail/connect", follow_redirects=False)
    assert started.status_code == 307, started.text
    location = httpx.URL(started.headers["location"])
    state = location.params["state"]
    nonce = json.loads(get_redis().get(f"crm:gmail:state:{state}"))["nonce"]  # type: ignore[arg-type]
    gmail["google"].token_response = {
        "id_token": gmail["google"].id_token(nonce=nonce, **claims),
        "refresh_token": "refresh-token-secret-1",
        "scope": "openid email https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly",
        "access_token": "a",
    }
    return location, client.get(
        f"{API}/mailboxes/gmail/callback", params={"code": "auth-code", "state": state}, follow_redirects=False
    )


def connected(owner: TestClient, gmail: dict[str, Any]) -> dict[str, Any]:
    _, response = connect(owner, gmail)
    assert response.status_code == 303, response.text
    return ok(owner.get(f"{API}/mailboxes"))[0]  # type: ignore[no-any-return]


def lead_with_email(owner: TestClient, email: str = PROSPECT) -> dict[str, Any]:
    import_and_commit(owner, workbook_bytes([lead_row("E-001", "Salon Aurora", **{"Public business email": email})]))
    return ok(owner.get(f"{API}/leads"))["items"][0]  # type: ignore[no-any-return]


def approve_policy(owner: TestClient) -> None:
    ok(
        owner.put(
            f"{API}/outreach-policy/sender-identity",
            json={"sender_identity": "SEWEB Ltd, Sofia, Bulgaria, office@seweb.example"},
        )
    )
    ok(
        owner.post(
            f"{API}/outreach-policy/approve",
            json={
                "approval_note": "Reviewed against the current Electronic Commerce Act by counsel on 2026-10-20",
                "source_checked_on": "2026-10-20",
                "register_max_age_days": 7,
                "label_text": "Непоискано търговско съобщение",
            },
        )
    )


def import_register(owner: TestClient, addresses: list[str], valid_days: int = 7) -> None:
    ok(
        owner.post(
            f"{API}/regulatory-sources",
            data={
                "name": "Opt-out register",
                "version": "2026-10-20",
                "valid_days": str(valid_days),
                "obtained_how": "Downloaded from the regulator portal by the owner",
            },
            files={"file": ("register.txt", "\n".join(addresses).encode(), "text/plain")},
        ),
        201,
    )


def classify(owner: TestClient, address: str, legal_form: str = "legal_person", context: str = "business") -> None:
    ok(
        owner.put(
            f"{API}/recipient-profiles",
            json={
                "address": address,
                "legal_form": legal_form,
                "context": context,
                "evidence": "Company register entry checked on 2026-10-20",
            },
        )
    )


def codes(draft: dict[str, Any]) -> list[str]:
    return [r["code"] for r in draft["eligibility"]["reasons"]]


# --- eligibility -----------------------------------------------------------------------------


def test_unsolicited_email_is_blocked_until_every_requirement_is_met(owner: TestClient, gmail: dict[str, Any]) -> None:
    lead = lead_with_email(owner)
    draft = ok(owner.post(f"{API}/email-drafts", json={"lead_id": lead["id"]}), 201)
    assert draft["to_address"] == PROSPECT and draft["kind"] == "unsolicited" and draft["status"] == "draft"
    assert draft["body_text"].startswith("Здравейте")  # prefilled from the research, editable
    # Nothing is in place yet: no mailbox, a draft policy, no classification, no register, no sender identity.
    assert draft["eligibility"]["outcome"] == "block"
    assert codes(draft) == [
        "no_mailbox",
        "policy_not_approved",
        "recipient_unclassified",
        "register_unavailable",
        "sender_identity_missing",
    ]
    assert draft["eligibility"]["checks"]["recipient"] == {
        "legal_form": "unknown",
        "context": "unknown",
        "evidence": None,
    }
    assert draft["eligibility"]["checks"]["register"] == {
        "result": "unavailable"
    }  # an absent register is never "clear"

    connected(owner, gmail)
    approve_policy(owner)
    draft = ok(owner.get(f"{API}/email-drafts/{draft['id']}"))
    assert codes(draft) == ["recipient_unclassified", "register_unavailable"] and draft["eligibility"][
        "policy_version"
    ].startswith("bg-2026-10-20-")
    rendered = draft["eligibility"]["rendered_body"]
    assert (
        "Непоискано търговско съобщение" in rendered
        and "SEWEB Ltd, Sofia" in rendered
        and "Отписване / Unsubscribe: http" in rendered
    )

    import_register(owner, ["someone-else@example.bg"])
    draft = ok(owner.get(f"{API}/email-drafts/{draft['id']}"))
    assert draft["eligibility"]["outcome"] == "review" and codes(draft) == [
        "recipient_unclassified"
    ]  # unknown goes to review, not through
    classify(owner, PROSPECT)
    draft = ok(owner.get(f"{API}/email-drafts/{draft['id']}"))
    assert draft["eligibility"]["outcome"] == "allow" and codes(draft) == []
    assert draft["eligibility"]["checks"]["register"]["result"] == "clear"


def test_register_match_staleness_and_recipient_type_block_sending(
    owner: TestClient, gmail: dict[str, Any], migrator_engine: Any
) -> None:
    lead = lead_with_email(owner, "owner.personal@abv.bg")
    connected(owner, gmail)
    approve_policy(owner)
    draft = ok(owner.post(f"{API}/email-drafts", json={"lead_id": lead["id"]}), 201)
    address = "owner.personal@abv.bg"
    # A free-mail domain is not a classification: the recipient is unknown until someone records evidence.
    assert draft["eligibility"]["checks"]["recipient"]["legal_form"] == "unknown" and "recipient_unclassified" in codes(
        draft
    )
    assert (
        owner.put(
            f"{API}/recipient-profiles", json={"address": address, "legal_form": "legal_person", "context": "business"}
        ).status_code
        == 422
    )

    import_register(owner, [address.upper()])
    classify(owner, address)
    assert codes(ok(owner.get(f"{API}/email-drafts/{draft['id']}"))) == ["on_opt_out_register"]
    import_register(owner, [])
    assert codes(ok(owner.get(f"{API}/email-drafts/{draft['id']}"))) == []

    for legal_form, context, expected in (
        ("natural_person", "consumer", ["consent_required"]),
        ("legal_person", "consumer", ["consent_required"]),
        ("sole_trader", "business", ["sole_trader_review"]),
        ("legal_person", "business", []),
    ):
        classify(owner, address, legal_form, context)
        assert codes(ok(owner.get(f"{API}/email-drafts/{draft['id']}"))) == expected, legal_form

    with migrator_engine.begin() as conn:  # the register on file gets old
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(gmail["tenant"])})
        conn.execute(
            text("UPDATE regulatory_sources SET obtained_at = now() - interval '8 days' WHERE superseded_at IS NULL")
        )
    stale = ok(owner.get(f"{API}/email-drafts/{draft['id']}"))
    assert stale["eligibility"]["outcome"] == "block" and codes(stale) == ["register_stale"]
    assert ok(owner.get(f"{API}/outreach-policy"))["opt_out_register"]["state"] == "stale"


def test_suppression_and_restrictions_always_apply(owner: TestClient, gmail: dict[str, Any]) -> None:
    lead = lead_with_email(owner)
    connected(owner, gmail)
    approve_policy(owner)
    import_register(owner, [])
    classify(owner, PROSPECT)
    draft = ok(owner.post(f"{API}/email-drafts", json={"lead_id": lead["id"]}), 201)
    assert draft["eligibility"]["outcome"] == "allow"

    suppression = ok(
        owner.post(f"{API}/email-suppressions", json={"value": PROSPECT.upper(), "reason": "opt_out"}), 201
    )
    assert codes(ok(owner.get(f"{API}/email-drafts/{draft['id']}"))) == ["suppressed"]
    assert owner.post(f"{API}/email-suppressions/{suppression['id']}/lift", json={"lift_note": "x"}).status_code == 422
    ok(
        owner.post(
            f"{API}/email-suppressions/{suppression['id']}/lift",
            json={"lift_note": "Asked to be contacted again by phone on 21 Oct"},
        )
    )
    assert codes(ok(owner.get(f"{API}/email-drafts/{draft['id']}"))) == []
    ok(owner.post(f"{API}/email-suppressions", json={"scope": "domain", "value": "example-salon.bg"}), 201)
    assert codes(ok(owner.get(f"{API}/email-drafts/{draft['id']}"))) == ["suppressed"]
    domain_rule = next(s for s in ok(owner.get(f"{API}/email-suppressions"))["items"] if s["scope"] == "domain")
    ok(owner.post(f"{API}/email-suppressions/{domain_rule['id']}/lift", json={"lift_note": "Entered by mistake"}))

    ok(
        owner.post(
            f"{API}/companies/{lead['company_id']}/restrictions",
            json={"channel_kind": "email", "reason": "Asked for no email"},
        ),
        201,
    )
    assert codes(ok(owner.get(f"{API}/email-drafts/{draft['id']}"))) == ["do_not_contact"]
    # Re-importing the same prospect list does not bring the address back into use.
    import_and_commit(owner, workbook_bytes([lead_row("E-001", "Salon Aurora", **{"Public business email": PROSPECT})]))
    assert codes(ok(owner.get(f"{API}/email-drafts/{draft['id']}"))) == ["do_not_contact"]
    with (
        session_scope(RlsContext(tenant_id=gmail["tenant"])) as db,
        pytest.raises(Exception, match="permission denied"),
    ):
        db.execute(text("DELETE FROM email_suppressions"))


def test_editing_a_draft_cannot_remove_the_required_identification(owner: TestClient, gmail: dict[str, Any]) -> None:
    lead = lead_with_email(owner)
    connected(owner, gmail)
    approve_policy(owner)
    import_register(owner, [])
    classify(owner, PROSPECT)
    draft = ok(owner.post(f"{API}/email-drafts", json={"lead_id": lead["id"]}), 201)
    edited = ok(
        owner.patch(
            f"{API}/email-drafts/{draft['id']}",
            json={"body_text": "Just a friendly note, nothing commercial here.", "subject": "Hi"},
        )
    )
    assert edited["version"] == 2 and edited["body_text"] == "Just a friendly note, nothing commercial here."
    rendered = edited["eligibility"]["rendered_body"]
    assert (
        rendered.startswith("Just a friendly note")
        and "Непоискано търговско съобщение" in rendered
        and "SEWEB Ltd" in rendered
    )
    assert edited["eligibility"]["outcome"] == "allow"
    # If the policy text itself is emptied, the message is blocked rather than sent unlabelled.
    with session_scope(RlsContext(tenant_id=gmail["tenant"])) as db:
        db.execute(
            text(
                "UPDATE outreach_policies SET rules = rules || '{\"label_text\": \"\"}'::jsonb WHERE status = 'approved'"
            )
        )
    assert codes(ok(owner.get(f"{API}/email-drafts/{draft['id']}"))) == ["label_missing"]
    assert (
        owner.post(
            f"{API}/email-drafts",
            json={"lead_id": lead["id"], "to_address": "guessed@example-salon.bg\nBcc: x@evil.example"},
        ).status_code
        == 422
    )


def test_requested_follow_up_needs_a_recorded_request_not_a_policy_bypass(
    owner: TestClient, gmail: dict[str, Any]
) -> None:
    lead = lead_with_email(owner)
    connected(owner, gmail)
    ok(
        owner.put(
            f"{API}/outreach-policy/sender-identity",
            json={"sender_identity": "SEWEB Ltd, Sofia, Bulgaria, office@seweb.example"},
        )
    )
    draft = ok(
        owner.post(
            f"{API}/email-drafts",
            json={
                "lead_id": lead["id"],
                "kind": "requested",
                "to_address": "manager@example-salon.bg",
                "subject": "The details you asked for",
                "body_text": "As discussed by phone.",
            },
        ),
        201,
    )
    assert draft["eligibility"]["outcome"] == "block" and codes(draft) == ["no_request_on_record"]
    # Reporting a call in which they asked for the details records the request and its scope.
    detail = ok(owner.get(f"{API}/prospects/{lead['id']}"))
    phone = next(c for c in detail["channels"] if c["kind"] == "phone")
    call = ok(owner.post(f"{API}/prospects/{lead['id']}/calls", json={"channel_id": phone["id"]}), 201)
    ok(
        owner.post(
            f"{API}/calls/{call['id']}/outcome",
            json={
                "outcome": "follow_up_requested",
                "follow_up_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "follow_up_scope": "Online booking demo details",
                "requested_email": "manager@example-salon.bg",
            },
        )
    )
    allowed = ok(owner.get(f"{API}/email-drafts/{draft['id']}"))
    assert allowed["eligibility"]["outcome"] == "allow"
    assert allowed["eligibility"]["checks"]["request_on_record"]["scope"] == "Online booking demo details"
    assert allowed["eligibility"]["checks"]["request_on_record"]["kind"] == "requested_follow_up"
    assert "Непоискано" not in allowed["eligibility"]["rendered_body"]  # not unsolicited, so not labelled as such
    # The same request does not make an unsolicited campaign message to that address allowed.
    cold = ok(
        owner.post(f"{API}/email-drafts", json={"lead_id": lead["id"], "to_address": "manager@example-salon.bg"}), 201
    )
    assert cold["eligibility"]["outcome"] == "block" and "policy_not_approved" in codes(cold)


def test_policy_approval_is_an_owner_decision_with_a_record(owner: TestClient, make_client: Any) -> None:
    policy = ok(owner.get(f"{API}/outreach-policy"))
    assert (
        policy["status"] == "draft"
        and policy["version"] == "bg-2026-10-draft"
        and policy["opt_out_register"]["state"] == "unavailable"
    )
    assert "unofficial" in policy["source_note"]
    body = {
        "approval_note": "Reviewed against the current Electronic Commerce Act by counsel on 2026-10-20",
        "source_checked_on": "2026-10-20",
        "register_max_age_days": 7,
        "label_text": "Непоискано търговско съобщение",
    }
    assert owner.post(f"{API}/outreach-policy/approve", json=body).status_code == 422  # sender identity first
    ok(owner.put(f"{API}/outreach-policy/sender-identity", json={"sender_identity": "SEWEB Ltd, Sofia, Bulgaria"}))
    assert owner.post(f"{API}/outreach-policy/approve", json={**body, "approval_note": "ok"}).status_code == 422
    admin = make_client()
    sign_in(admin, "admin@example.test")
    join(owner, admin, "admin@example.test", "administrator")
    assert admin.post(f"{API}/outreach-policy/approve", json=body).status_code == 403
    approved = ok(owner.post(f"{API}/outreach-policy/approve", json=body))
    assert approved["status"] == "approved" and approved["approval_note"].startswith("Reviewed against")
    again = ok(owner.post(f"{API}/outreach-policy/approve", json={**body, "register_max_age_days": 3}))
    assert again["version"] != approved["version"] and again["rules"]["register_max_age_days"] == 3
    assert "outreach_policy.approved" in [e["action"] for e in ok(owner.get(f"{API}/audit-events"))["items"]]


def test_opt_out_links_are_signed_and_a_scanner_cannot_trigger_them(
    owner: TestClient, gmail: dict[str, Any], client: TestClient
) -> None:
    token = make_token(gmail["tenant"], PROSPECT)
    assert read_token(token) == (gmail["tenant"], PROSPECT)
    assert read_token(token[:-2] + "AA") is None and read_token("not-a-token") is None
    forged = make_token(gmail["tenant"], "someone.else@example.bg")[:22] + token[22:]
    assert read_token(forged) is None
    # Opening the link (as a mail scanner would) changes nothing; it shows a button.
    page = client.get(f"{API}/unsubscribe/{token}")
    assert (
        page.status_code == 200
        and '<form method="post">' in page.text
        and ok(owner.get(f"{API}/email-suppressions"))["total"] == 0
    )
    assert client.post(f"{API}/unsubscribe/{token}").status_code == 200  # one-click, no session, no CSRF token
    listed = ok(owner.get(f"{API}/email-suppressions"))["items"]
    assert [(s["value"], s["reason"], s["source"]) for s in listed] == [(PROSPECT, "opt_out", "unsubscribe_link")]
    assert (
        client.post(f"{API}/unsubscribe/{token}").status_code == 200
        and ok(owner.get(f"{API}/email-suppressions"))["total"] == 1
    )
    assert client.post(f"{API}/unsubscribe/{'A' * 60}").status_code == 404


# --- connecting Gmail ------------------------------------------------------------------------


def test_internal_gmail_connection_stores_only_an_encrypted_token(
    owner: TestClient, gmail: dict[str, Any], migrator_engine: Any
) -> None:
    assert ok(owner.get(f"{API}/mailboxes/gmail/availability")) == {
        "available": True,
        "reason": None,
        "internal_domain": DOMAIN,
    }
    location, response = connect(owner, gmail)
    assert (
        location.host == "accounts.google.com"
        and location.params["hd"] == DOMAIN
        and location.params["access_type"] == "offline"
    )
    assert set(location.params["scope"].split()) == {
        "openid",
        "email",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
    }
    assert "gmail.modify" not in location.params["scope"]  # mailbox mutation is not requested
    assert response.status_code == 303 and response.headers["location"].endswith("/integrations")
    box = ok(owner.get(f"{API}/mailboxes"))[0]
    assert (box["email_address"], box["mode"], box["status"], box["verification"], box["can_read_replies"]) == (
        MAILBOX,
        "internal",
        "pending",
        "implemented",
        True,
    )
    assert "refresh-token" not in json.dumps(ok(owner.get(f"{API}/mailboxes"))) and "refresh-token" not in json.dumps(
        ok(owner.get(f"{API}/audit-events"))
    )
    with migrator_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(gmail["tenant"])})
        stored = conn.execute(text("SELECT token_ciphertext, token_key_version FROM mailboxes")).one()
        jobs = sorted(
            r[0]
            for r in conn.execute(text("SELECT kind FROM due_jobs WHERE kind LIKE 'mailbox.%' AND status = 'pending'"))
        )
    assert b"refresh-token" not in bytes(stored.token_ciphertext) and stored.token_key_version
    assert jobs == ["mailbox.sync", "mailbox.watch"]
    # Reconnecting the same address keeps the same mailbox record.
    connect(owner, gmail)
    assert [m["id"] for m in ok(owner.get(f"{API}/mailboxes"))] == [box["id"]]


@pytest.mark.parametrize(
    ("claims", "message"),
    [
        ({"hd": "other-company.example"}, "Only verified seweb.example accounts"),
        ({"hd": None, "email": "someone@gmail.com"}, "Only verified seweb.example accounts"),
        (
            {"hd": None, "email": f"spoof@{DOMAIN}"},
            "Only verified seweb.example accounts",
        ),  # the email suffix alone is not trusted
        ({"email_verified": False}, "Only verified seweb.example accounts"),
        ({"aud": "another-client"}, "could not be verified"),
        ({"iss": "https://evil.example"}, "could not be verified"),
        ({"exp": int(time.time()) - 3600}, "could not be verified"),
        ({"nonce": "wrong"}, "could not be verified"),
    ],
    ids=[
        "other-org",
        "consumer-account",
        "spoofed-suffix",
        "unverified-email",
        "wrong-audience",
        "wrong-issuer",
        "expired",
        "wrong-nonce",
    ],
)
def test_accounts_outside_the_organisation_cannot_connect(
    owner: TestClient, gmail: dict[str, Any], claims: dict[str, Any], message: str
) -> None:
    started = owner.get(f"{API}/mailboxes/gmail/connect", follow_redirects=False)
    state = httpx.URL(started.headers["location"]).params["state"]
    nonce = json.loads(get_redis().get(f"crm:gmail:state:{state}"))["nonce"]  # type: ignore[arg-type]
    gmail["google"].token_response = {
        "id_token": gmail["google"].id_token(**{"nonce": nonce, **claims}),
        "refresh_token": "r",
        "scope": "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly",
    }
    response = owner.get(
        f"{API}/mailboxes/gmail/callback", params={"code": "c", "state": state}, follow_redirects=False
    )
    assert response.status_code == 409 and message in response.json()["error"]["message"]
    assert ok(owner.get(f"{API}/mailboxes")) == []


def test_oauth_misuse_is_refused(owner: TestClient, gmail: dict[str, Any], make_client: Any) -> None:
    started = owner.get(f"{API}/mailboxes/gmail/connect", follow_redirects=False)
    state = httpx.URL(started.headers["location"]).params["state"]
    gmail["google"].token_response = {"id_token": gmail["google"].id_token(), "refresh_token": "r", "scope": "x"}
    assert (
        owner.get(
            f"{API}/mailboxes/gmail/callback", params={"code": "c", "state": "another-state"}, follow_redirects=False
        ).status_code
        == 401
    )
    assert (
        owner.get(f"{API}/mailboxes/gmail/callback", params={"state": state}, follow_redirects=False).status_code == 401
    )

    # A callback started by one user cannot be finished by another, even in the same workspace.
    admin = make_client()
    sign_in(admin, "admin@example.test")
    join(owner, admin, "admin@example.test", "administrator")
    admin.cookies.set("crm_gmail_state", state)
    assert (
        admin.get(
            f"{API}/mailboxes/gmail/callback", params={"code": "c", "state": state}, follow_redirects=False
        ).status_code
        == 401
    )
    # The state is single-use: the first attempt consumed it.
    assert (
        owner.get(
            f"{API}/mailboxes/gmail/callback", params={"code": "c", "state": state}, follow_redirects=False
        ).status_code
        == 401
    )

    # Missing read permission, or no long-lived token, is refused with a reason.
    for response_body, message in (
        (
            {"scope": "https://www.googleapis.com/auth/gmail.send", "refresh_token": "r"},
            "Both sending and reading permission",
        ),
        (
            {"scope": "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly"},
            "did not return long-lived access",
        ),
    ):
        started = owner.get(f"{API}/mailboxes/gmail/connect", follow_redirects=False)
        state = httpx.URL(started.headers["location"]).params["state"]
        nonce = json.loads(get_redis().get(f"crm:gmail:state:{state}"))["nonce"]  # type: ignore[arg-type]
        gmail["google"].token_response = {"id_token": gmail["google"].id_token(nonce=nonce), **response_body}
        refused = owner.get(
            f"{API}/mailboxes/gmail/callback", params={"code": "c", "state": state}, follow_redirects=False
        )
        assert refused.status_code == 409 and message in refused.json()["error"]["message"]
    rep = make_client()
    sign_in(rep, "rep@example.test")
    join(owner, rep, "rep@example.test", "representative")
    assert rep.get(f"{API}/mailboxes/gmail/connect", follow_redirects=False).status_code == 403
    assert ok(owner.get(f"{API}/mailboxes")) == []


def test_another_tenant_cannot_use_the_internal_google_app(
    owner: TestClient, gmail: dict[str, Any], make_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = make_client()
    sign_in(other, "other@example.test")
    create_workspace(other, "Another customer")
    availability = ok(other.get(f"{API}/mailboxes/gmail/availability"))
    assert availability["available"] is False and "only to the organisation that owns" in availability["reason"]
    assert other.get(f"{API}/mailboxes/gmail/connect", follow_redirects=False).status_code == 409
    # Even with a state minted for the owner's flow, the callback re-checks the tenant.
    started = owner.get(f"{API}/mailboxes/gmail/connect", follow_redirects=False)
    state = httpx.URL(started.headers["location"]).params["state"]
    other.cookies.set("crm_gmail_state", state)
    assert (
        other.get(
            f"{API}/mailboxes/gmail/callback", params={"code": "c", "state": state}, follow_redirects=False
        ).status_code
        == 401
    )
    # Turning on the external flag does not open a back door: external Gmail is not implemented.
    monkeypatch.setattr(get_settings(), "external_gmail_enabled", True)
    assert other.get(f"{API}/mailboxes/gmail/connect", follow_redirects=False).status_code == 409
    assert ok(other.get(f"{API}/mailboxes")) == []
    monkeypatch.setattr(get_settings(), "gmail_client_id", "")
    assert "not configured" in ok(owner.get(f"{API}/mailboxes/gmail/availability"))["reason"]


def test_the_external_gmail_gate_needs_every_recorded_prerequisite_and_still_stays_closed(
    owner: TestClient, gmail: dict[str, Any], make_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date

    settings = get_settings()
    other = make_client()
    sign_in(other, "other@example.test")
    create_workspace(other, "Another customer")

    def closed(expected: str) -> None:
        reason = ok(other.get(f"{API}/mailboxes/gmail/availability"))["reason"]
        assert expected in reason, reason
        assert other.get(f"{API}/mailboxes/gmail/connect", follow_redirects=False).status_code == 409
        started = owner.get(f"{API}/mailboxes/gmail/connect", follow_redirects=False)
        state = httpx.URL(started.headers["location"]).params["state"]
        other.cookies.set("crm_gmail_state", state)
        assert (
            other.get(
                f"{API}/mailboxes/gmail/callback", params={"code": "c", "state": state}, follow_redirects=False
            ).status_code
            == 401
        )
        assert ok(other.get(f"{API}/mailboxes")) == []

    assert len(oauth.external_gmail_gate(settings)) == 4
    closed("the release flag is off")
    monkeypatch.setattr(settings, "external_gmail_enabled", True)
    closed("no separately verified Google app")
    monkeypatch.setattr(settings, "external_gmail_client_id", CLIENT_ID)  # the internal app, reused
    closed("the internal Google app cannot be used for other organisations")
    monkeypatch.setattr(settings, "external_gmail_client_id", "external-client.apps.example")
    closed("verification of the sensitive and restricted scopes is not recorded")
    monkeypatch.setattr(settings, "external_gmail_verification_ref", "recorded-reference")
    closed("no security assessment is recorded")
    monkeypatch.setattr(settings, "external_gmail_assessment_valid_until", date(2026, 1, 1))
    closed("the security assessment expired on 2026-01-01")
    monkeypatch.setattr(settings, "external_gmail_assessment_valid_until", date(2099, 1, 1))
    assert oauth.external_gmail_gate(settings) == []
    closed("not implemented yet")  # the gate can be open on paper; the flow is still not built
    assert ok(owner.get(f"{API}/mailboxes/gmail/availability"))["available"] is True  # the internal pilot is unaffected


# --- synchronisation -------------------------------------------------------------------------


def state_of(gmail: dict[str, Any]) -> Any:
    with session_scope(RlsContext(tenant_id=gmail["tenant"])) as db:
        return db.execute(text("SELECT * FROM mailboxes WHERE tenant_id = :t"), {"t": gmail["tenant"]}).mappings().one()


def run_sync(gmail: dict[str, Any]) -> dict[str, Any]:
    return sync.sync_mailbox(gmail["tenant"], state_of(gmail)["id"])


def test_first_sync_is_a_resumable_full_sync_that_loses_nothing(owner: TestClient, gmail: dict[str, Any]) -> None:
    box: FakeMailbox = gmail["box"]
    for n in range(5):
        box.add(sender=f"person{n}@example.bg", subject=f"Message {n}")
    connected(owner, gmail)
    box.fail["list_messages"] = [MailboxError("The mailbox provider returned a server error.")] if False else []
    box.fail["get_message"] = [MailboxError("The mailbox provider timed out.")]  # crash partway through page 1
    cursor_at_start = str(box.history_id)
    first = run_sync(gmail)
    assert first["mode"] == "full" and "error" in first
    state = state_of(gmail)
    assert (
        state["full_sync_required"]
        and state["full_sync_started_cursor"] == cursor_at_start
        and state["history_cursor"] is None
    )
    assert state["status"] == "degraded" and state["backoff_until"] is not None
    late = box.add(
        sender="late@example.bg", subject="Arrived during the full sync"
    )  # after the start cursor was captured

    with session_scope(RlsContext(tenant_id=gmail["tenant"])) as db:
        db.execute(text("UPDATE mailboxes SET backoff_until = NULL"))
    second = run_sync(gmail)
    assert second["mode"] == "full" and "error" not in second and second["stored"] == 6
    state = state_of(gmail)
    # The cursor is the one captured before listing began, so the late message is covered by the next incremental pass.
    assert (state["history_cursor"], state["full_sync_required"], state["status"]) == (cursor_at_start, False, "active")
    third = run_sync(gmail)
    assert third["mode"] == "incremental" and third["stored"] == 0  # already stored by the listing: no duplicate
    assert state_of(gmail)["history_cursor"] == str(box.history_id)
    with session_scope(RlsContext(tenant_id=gmail["tenant"])) as db:
        assert db.execute(text("SELECT count(*), count(DISTINCT provider_message_id) FROM email_messages")).one() == (
            6,
            6,
        )
    assert late.id in box.messages


def test_incremental_sync_commits_each_page_before_moving_the_cursor(owner: TestClient, gmail: dict[str, Any]) -> None:
    box: FakeMailbox = gmail["box"]
    connected(owner, gmail)
    run_sync(gmail)
    start = int(state_of(gmail)["history_cursor"])
    for n in range(5):  # three pages of two
        box.add(sender=f"reply{n}@example.bg", subject=f"Reply {n}")
    # The process dies while fetching the third message (the first message of page two).
    box.fail["get_message"] = []
    original = box.get_message
    seen: list[str] = []

    def flaky(token: str, message_id: str) -> Any:
        seen.append(message_id)
        if len(seen) == 3:
            raise MailboxError("The mailbox provider timed out.")
        return original(token, message_id)

    box.get_message = flaky  # type: ignore[method-assign]
    crashed = run_sync(gmail)
    assert "error" in crashed and crashed["stored"] == 2 and crashed["pages"] == 1
    state = state_of(gmail)
    assert int(state["history_cursor"]) == start + 2  # past page one only; page two will be read again
    box.get_message = original  # type: ignore[method-assign]
    with session_scope(RlsContext(tenant_id=gmail["tenant"])) as db:
        db.execute(text("UPDATE mailboxes SET backoff_until = NULL"))
    resumed = run_sync(gmail)
    assert "error" not in resumed and resumed["stored"] == 3
    assert int(state_of(gmail)["history_cursor"]) == box.history_id
    with session_scope(RlsContext(tenant_id=gmail["tenant"])) as db:
        assert db.execute(text("SELECT count(*), count(DISTINCT provider_message_id) FROM email_messages")).one() == (
            5,
            5,
        )
    assert run_sync(gmail)["stored"] == 0  # replaying is harmless


def test_an_expired_cursor_falls_back_to_a_full_sync(owner: TestClient, gmail: dict[str, Any]) -> None:
    box: FakeMailbox = gmail["box"]
    connected(owner, gmail)
    box.add(sender="early@example.bg")
    run_sync(gmail)
    box.add(sender="while-away@example.bg", subject="Sent during a long outage")
    box.oldest_history = box.history_id + 1  # the provider no longer has history that far back (HTTP 404)
    with pytest.raises(HistoryExpired):
        box.list_history("t", state_of(gmail)["history_cursor"], None)
    result = run_sync(gmail)
    assert result["mode"] == "full_after_expired_cursor" and result["stored"] == 1 and "error" not in result
    state = state_of(gmail)
    assert not state["full_sync_required"] and state["history_cursor"] == str(box.history_id)
    with session_scope(RlsContext(tenant_id=gmail["tenant"])) as db:
        assert db.execute(text("SELECT count(*) FROM email_messages")).scalar() == 2


def test_notifications_are_only_triggers_and_dropped_ones_are_recovered(
    owner: TestClient, gmail: dict[str, Any], client: TestClient
) -> None:
    box: FakeMailbox = gmail["box"]
    mailbox = connected(owner, gmail)
    run_sync(gmail)
    cursor = state_of(gmail)["history_cursor"]
    google = gmail["google"]

    def push(history_id: int, message_id: str, *, token: str | None = None, address: str = MAILBOX) -> int:
        now = int(time.time())
        bearer = token or jwt.encode(
            {
                "iss": "https://accounts.google.com",
                "aud": "https://crm.example/api/v1/webhooks/gmail",
                "sub": "svc",
                "email": "gmail-push@p.iam.gserviceaccount.example",
                "email_verified": True,
                "iat": now,
                "exp": now + 300,
            },
            google.key,
            algorithm="RS256",
            headers={"kid": "g1"},
        )
        data = base64.b64encode(json.dumps({"emailAddress": address, "historyId": history_id}).encode()).decode()
        return client.post(
            f"{API}/webhooks/gmail",
            json={"message": {"data": data, "messageId": message_id}},
            headers={"Authorization": f"Bearer {bearer}"},
        ).status_code

    # A forged or misdirected notification is refused.
    assert client.post(f"{API}/webhooks/gmail", json={"message": {}}).status_code == 401
    now = int(time.time())
    for bad in (
        jwt.encode(
            {
                "iss": "https://accounts.google.com",
                "aud": "https://somewhere-else.example",
                "sub": "s",
                "email": "gmail-push@p.iam.gserviceaccount.example",
                "email_verified": True,
                "iat": now,
                "exp": now + 300,
            },
            google.key,
            algorithm="RS256",
            headers={"kid": "g1"},
        ),
        jwt.encode(
            {
                "iss": "https://accounts.google.com",
                "aud": "https://crm.example/api/v1/webhooks/gmail",
                "sub": "s",
                "email": "attacker@evil.example",
                "email_verified": True,
                "iat": now,
                "exp": now + 300,
            },
            google.key,
            algorithm="RS256",
            headers={"kid": "g1"},
        ),
        jwt.encode(
            {"aud": "https://crm.example/api/v1/webhooks/gmail", "email": "gmail-push@p.iam.gserviceaccount.example"},
            "a-shared-secret-that-is-long-enough-for-hs256",
            algorithm="HS256",
        ),
    ):
        assert push(1, "forged", token=bad) == 401

    # A notification carrying a far-future history ID must not move the cursor.
    first_id = f"push-{uuid4()}"  # ids are remembered, so each run uses its own
    box.add(sender="first@example.bg")
    assert push(99_999_999, first_id) == 204
    after_push = state_of(gmail)
    assert after_push["history_cursor"] == cursor and after_push["last_notification_at"] is not None
    job = next(j for j in due.claim_due(20) if j["kind"] == "mailbox.sync")  # the notification made the sync due now
    assert due.run_claimed(job) == "rescheduled"
    assert state_of(gmail)["history_cursor"] == str(box.history_id)
    assert push(99_999_999, first_id) == 204  # delivered twice: ignored
    assert push(5, f"push-{uuid4()}", address="stranger@elsewhere.example") == 204  # unknown mailbox: nothing happens

    # Two more messages arrive and their notifications are lost. The timed reconciliation still finds them.
    box.add(sender="second@example.bg")
    box.add(sender="third@example.bg")
    assert due.claim_due(20) == [] or all(j["kind"] != "mailbox.sync" for j in due.claim_due(20))
    with session_scope(RlsContext(tenant_id=gmail["tenant"])) as db:
        due_at = db.execute(text("SELECT due_at FROM due_jobs WHERE kind = 'mailbox.sync'")).scalar_one()
        assert (
            timedelta(minutes=14) < due_at - datetime.now(UTC) <= timedelta(minutes=15)
        )  # every 15 minutes, quiet or not
        db.execute(text("UPDATE due_jobs SET due_at = now() WHERE kind = 'mailbox.sync'"))
    job = next(j for j in due.claim_due(20) if j["kind"] == "mailbox.sync")
    assert due.run_claimed(job) == "rescheduled"
    with session_scope(RlsContext(tenant_id=gmail["tenant"])) as db:
        assert db.execute(text("SELECT count(*) FROM email_messages")).scalar() == 3
    assert ok(owner.get(f"{API}/mailboxes"))[0]["healthy"] is True and mailbox["id"]


def test_only_one_sync_runs_per_mailbox_and_rate_limits_back_off(owner: TestClient, gmail: dict[str, Any]) -> None:
    box: FakeMailbox = gmail["box"]
    connected(owner, gmail)
    run_sync(gmail)
    mailbox_id = state_of(gmail)["id"]
    held = sync._acquire(gmail["tenant"], mailbox_id)  # another worker is mid-sync
    assert held is not None and run_sync(gmail) == {"skipped": "busy, backing off or revoked"}
    sync._release(gmail["tenant"], mailbox_id, held[1])

    box.add(sender="someone@example.bg")
    box.fail["list_history"] = [
        MailboxError("The mailbox provider is rate limiting requests.", rate_limited=True, retry_after=120)
    ]
    limited = run_sync(gmail)
    state = state_of(gmail)
    assert "rate limiting" in limited["error"] and state["status"] == "degraded" and state["consecutive_failures"] == 1
    assert timedelta(seconds=110) < state["backoff_until"] - datetime.now(UTC) <= timedelta(seconds=120)
    assert run_sync(gmail) == {"skipped": "busy, backing off or revoked"}  # respects the provider's back-off
    health = ok(owner.get(f"{API}/mailboxes"))[0]
    assert health["healthy"] is False and any("slow down" in p for p in health["problems"])

    with session_scope(RlsContext(tenant_id=gmail["tenant"])) as db:
        db.execute(text("UPDATE mailboxes SET backoff_until = NULL, last_synced_at = now() - interval '45 minutes'"))
    assert any("overdue" in p for p in ok(owner.get(f"{API}/mailboxes"))[0]["problems"])
    assert run_sync(gmail)["stored"] == 1 and ok(owner.get(f"{API}/mailboxes"))[0]["healthy"] is True


def test_watch_renewal_failure_raises_an_alert_before_expiry_and_never_moves_the_cursor(
    owner: TestClient, gmail: dict[str, Any]
) -> None:
    box: FakeMailbox = gmail["box"]
    connected(owner, gmail)
    run_sync(gmail)
    mailbox_id = state_of(gmail)["id"]
    box.add(sender="unprocessed@example.bg")  # history the sync has not read yet
    cursor = state_of(gmail)["history_cursor"]
    box.watch_expiry = datetime.now(UTC) + timedelta(days=6)
    assert sync.renew_watch(gmail["tenant"], mailbox_id)["renewed"] is True
    state = state_of(gmail)
    assert state["history_cursor"] == cursor  # renewing returns a newer history ID; it is not adopted
    assert state["watch_expires_at"] == box.watch_expiry and state["alert"] is None
    assert run_sync(gmail)["stored"] == 1  # the older unread history is still processed

    # Renewal fails while expiry is days away: recorded, no alert yet.
    box.fail["watch"] = [MailboxError("The mailbox provider returned a server error.")]
    assert sync.renew_watch(gmail["tenant"], mailbox_id) == {
        "renewed": False,
        "alert": False,
        "error": "The mailbox provider returned a server error.",
    }
    assert state_of(gmail)["alert"] is None and state_of(gmail)["watch_renewal_failed_at"] is not None
    # Within 24 hours of expiry a failed renewal becomes a visible alert.
    with session_scope(RlsContext(tenant_id=gmail["tenant"])) as db:
        db.execute(text("UPDATE mailboxes SET watch_expires_at = now() + interval '20 hours'"))
    box.fail["watch"] = [MailboxError("The mailbox provider returned a server error.")]
    assert sync.renew_watch(gmail["tenant"], mailbox_id)["alert"] is True
    health = ok(owner.get(f"{API}/mailboxes"))[0]
    assert health["status"] == "degraded" and any(
        "Push notifications could not be renewed" in p for p in health["problems"]
    )
    assert sync.renew_watch(gmail["tenant"], mailbox_id)["renewed"] is True and state_of(gmail)["alert"] is None
    # The renewal itself is a daily due row, retried within the hour after a failure.
    with session_scope(RlsContext(tenant_id=gmail["tenant"])) as db:
        db.execute(text("UPDATE due_jobs SET due_at = now() WHERE kind = 'mailbox.watch'"))
        box.fail["watch"] = [MailboxError("down")]
    job = next(j for j in due.claim_due(20) if j["kind"] == "mailbox.watch")
    assert due.run_claimed(job) == "rescheduled"
    with session_scope(RlsContext(tenant_id=gmail["tenant"])) as db:
        soon = db.execute(text("SELECT due_at FROM due_jobs WHERE kind = 'mailbox.watch'")).scalar_one()
    assert timedelta(minutes=59) < soon - datetime.now(UTC) <= timedelta(minutes=61)


def test_a_withdrawn_authorisation_is_shown_and_reconnecting_keeps_history(
    owner: TestClient, gmail: dict[str, Any]
) -> None:
    box: FakeMailbox = gmail["box"]
    connected(owner, gmail)
    box.add(sender="before@example.bg")
    run_sync(gmail)
    cursor = state_of(gmail)["history_cursor"]
    gmail["google"].refresh_status = 400  # the user removed the app's access in their Google account
    sync.set_provider(box, sync.TokenSource())
    revoked = run_sync(gmail)
    assert "withdrawn or expired" in revoked["error"]
    health = ok(owner.get(f"{API}/mailboxes"))[0]
    assert health["status"] == "revoked" and any(
        "Reconnect the mailbox; its history is kept" in p for p in health["problems"]
    )
    assert run_sync(gmail) == {"skipped": "busy, backing off or revoked"}

    gmail["google"].refresh_status = 200
    sync.set_provider(box, Tokens())
    box.add(sender="while-disconnected@example.bg")
    connect(owner, gmail)
    state = state_of(gmail)
    assert state["status"] == "pending" and state["history_cursor"] == cursor  # same record, same position
    assert run_sync(gmail)["stored"] == 1
    with session_scope(RlsContext(tenant_id=gmail["tenant"])) as db:
        assert db.execute(text("SELECT count(*) FROM email_messages")).scalar() == 2
    disconnected = ok(owner.delete(f"{API}/mailboxes/{state['id']}"))
    assert disconnected["status"] == "revoked" and state_of(gmail)["token_ciphertext"] is None
    with session_scope(RlsContext(tenant_id=gmail["tenant"])) as db:
        assert db.execute(text("SELECT count(*) FROM email_messages")).scalar() == 2  # conversations are kept


def test_disconnecting_withdraws_the_authorisation_at_google_and_says_when_it_could_not(
    owner: TestClient, gmail: dict[str, Any]
) -> None:
    box: FakeMailbox = gmail["box"]
    first = connected(owner, gmail)
    ok(owner.delete(f"{API}/mailboxes/{first['id']}"))
    # Notifications are stopped and the grant itself is revoked, not just forgotten locally.
    assert box.calls.count("stop") == 1 and gmail["google"].revoked == ["token=refresh-token-secret-1"]
    event = next(e for e in ok(owner.get(f"{API}/audit-events"))["items"] if e["action"] == "mailbox.disconnected")
    assert "not_released_at_provider" not in event["data"]

    # Google is unreachable: the disconnect still happens here, and the record says what is left to do by hand.
    connect(owner, gmail)
    gmail["google"].revoke_status = 503
    box.fail["stop"] = [MailboxError("The mailbox provider could not be reached.")]
    again = ok(owner.delete(f"{API}/mailboxes/{first['id']}"))
    assert again["status"] == "revoked" and state_of(gmail)["token_ciphertext"] is None
    latest = next(e for e in ok(owner.get(f"{API}/audit-events"))["items"] if e["action"] == "mailbox.disconnected")
    problems = latest["data"]["not_released_at_provider"]
    assert len(problems) == 2 and any("remove the app in the Google account" in p for p in problems)
    assert "refresh-token" not in json.dumps(ok(owner.get(f"{API}/audit-events")))

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    GmailProvider(httpx.Client(transport=httpx.MockTransport(handler))).stop("access-token")
    assert calls[0].method == "POST" and str(calls[0].url).endswith("/gmail/v1/users/me/stop")
    assert GoogleAuth(get_settings(), httpx.Client(transport=httpx.MockTransport(handler))).revoke("r-token") is True
    assert str(calls[1].url) == "https://oauth2.googleapis.com/revoke" and calls[1].content == b"token=r-token"


# --- conversations ---------------------------------------------------------------------------


def test_replies_link_to_the_right_record_and_ambiguous_ones_wait_for_a_person(
    owner: TestClient, gmail: dict[str, Any]
) -> None:
    box: FakeMailbox = gmail["box"]
    import_and_commit(
        owner,
        workbook_bytes(
            [
                lead_row("E-001", "Salon Aurora", **{"Public business email": PROSPECT}),
                lead_row("E-002", "Vet Clinic", **{"Public business email": "shared@group.example.bg"}),
                lead_row("E-003", "Vet Clinic Two", **{"Public business email": "shared@group.example.bg"}),
            ]
        ),
    )
    leads = {item["external_id"]: item for item in ok(owner.get(f"{API}/leads"))["items"]}
    connected(owner, gmail)
    sent = box.add(
        sender=MAILBOX, to=PROSPECT, subject="Онлайн резервации", labels=("SENT",), message_id="<out-1@seweb.example>"
    )
    box.add(
        sender=f"Мария <{PROSPECT.upper()}>",
        subject="Re: Онлайн резервации",
        thread_id=sent.thread_id,
        text="Здравейте, интересно ни е.",
        headers={"In-Reply-To": "<out-1@seweb.example>", "References": "<out-1@seweb.example>"},
    )
    box.add(sender="shared@group.example.bg", subject="Question about your offer")
    box.add(sender="stranger@unknown.example", subject="Partnership proposal")
    box.add(sender=PROSPECT, subject="Automatic reply: Онлайн резервации", headers={"Auto-Submitted": "auto-replied"})
    run_sync(gmail)

    linked = ok(owner.get(f"{API}/email-threads", params={"lead_id": leads["E-001"]["id"]}))["items"]
    conversation = next(t for t in linked if len(t["messages"]) == 2)
    assert [(m["direction"], m["classification"]) for m in conversation["messages"]] == [
        ("outbound", "message"),
        ("inbound", "reply"),
    ]
    assert conversation["link_state"] == "linked" and conversation["messages"][1]["from_address"] == PROSPECT
    assert ok(owner.get(f"{API}/leads/{leads['E-001']['id']}"))["outreach_status"] == "replied"
    timeline = [
        a["kind"] for a in ok(owner.get(f"{API}/activities", params={"lead_id": leads["E-001"]["id"]}))["items"]
    ]
    assert timeline.count("email.received") == 1  # the automatic reply did not count as a reply
    auto = next(t for t in linked if t["messages"][0]["classification"] == "out_of_office")
    assert auto["link_state"] == "linked"

    review = ok(owner.get(f"{API}/email-threads", params={"needs_review": True}))["items"]
    by_sender = {t["messages"][0]["from_address"]: t for t in review}
    assert set(by_sender) == {"shared@group.example.bg", "stranger@unknown.example"}
    assert (
        by_sender["shared@group.example.bg"]["link_state"] == "conflict"
        and "2 companies" in by_sender["shared@group.example.bg"]["link_note"]
    )
    assert by_sender["stranger@unknown.example"]["link_state"] == "unmatched"
    assert (
        ok(owner.get(f"{API}/leads/{leads['E-002']['id']}"))["outreach_status"] == "not_contacted"
    )  # nobody was guessed
    chosen = ok(
        owner.post(
            f"{API}/email-threads/{by_sender['shared@group.example.bg']['id']}/link",
            json={"lead_id": leads["E-003"]["id"]},
        )
    )
    assert chosen["link_state"] == "linked" and chosen["lead_id"] == leads["E-003"]["id"]
    ignored = ok(
        owner.post(f"{API}/email-threads/{by_sender['stranger@unknown.example']['id']}/link", json={"ignore": True})
    )
    assert (
        ignored["link_state"] == "ignored"
        and ok(owner.get(f"{API}/email-threads", params={"needs_review": True}))["total"] == 0
    )


def test_incoming_html_is_sanitised_and_attachments_are_not_downloaded(
    owner: TestClient, gmail: dict[str, Any]
) -> None:
    box: FakeMailbox = gmail["box"]
    lead = lead_with_email(owner)
    connected(owner, gmail)
    hostile = (
        '<div onclick="steal()">Hello <b>there</b><script>alert(document.cookie)</script><img src="https://tracker.example/p.gif">'
        '<a href="javascript:alert(1)">bad</a> <a href="https://example-salon.bg/menu" onmouseover="x()">menu</a>'
        '<iframe src="https://evil.example"></iframe><form action="https://evil.example"><input name="password"></form>'
        "<style>body{background:url(https://tracker.example/bg)}</style></div>"
    )
    box.add(
        sender=PROSPECT,
        subject="Our menu",
        text="Hello there",
        html=hostile,
        attachments=[{"filename": "invoice.pdf.exe", "mime_type": "application/octet-stream", "size": 48_213}],
    )
    run_sync(gmail)
    message = ok(owner.get(f"{API}/email-threads", params={"lead_id": lead["id"]}))["items"][0]["messages"][0]
    html = message["body_html"]
    for forbidden in (
        "script",
        "onclick",
        "onmouseover",
        "javascript:",
        "iframe",
        "<form",
        "<input",
        "tracker.example",
        "<img",
        "<style",
    ):
        assert forbidden not in html, forbidden
    assert '<a href="https://example-salon.bg/menu" rel="noopener noreferrer nofollow" target="_blank">menu</a>' in html
    assert "<b>there</b>" in html and "[image removed]" in html and message["has_remote_content"] is True
    assert message["attachments"] == [
        {"filename": "invoice.pdf.exe", "mime_type": "application/octet-stream", "size": 48_213}
    ]
    assert "get_attachment" not in box.calls and message["body_text"] == "Hello there"
    assert mime.sanitize_html("<p>plain</p>") == ("<p>plain</p>", False)


def test_a_permanent_bounce_suppresses_the_address_and_a_temporary_one_does_not(
    owner: TestClient, gmail: dict[str, Any]
) -> None:
    box: FakeMailbox = gmail["box"]
    lead_with_email(owner)
    connected(owner, gmail)
    box.add(
        sender="mailer-daemon@googlemail.example",
        subject="Delivery Status Notification (Delay)",
        text="Temporary failure. Status: 4.4.1 connection timed out",
        headers={"X-Failed-Recipients": "busy@example.bg"},
    )
    box.add(
        sender="mailer-daemon@googlemail.example",
        subject="Delivery Status Notification (Failure)",
        text="Address not found. Status: 5.1.1 user unknown",
        headers={"X-Failed-Recipients": PROSPECT},
    )
    run_sync(gmail)
    suppressed = ok(owner.get(f"{API}/email-suppressions"))["items"]
    assert [(s["value"], s["reason"], s["source"]) for s in suppressed] == [(PROSPECT, "permanent_bounce", "mailbox")]
    assert mime.classify({}, from_address="postmaster@x.example", subject="x", content_type="text/plain") == "bounce"
    assert (
        mime.classify(
            {"precedence": "bulk"}, from_address="news@x.example", subject="Weekly", content_type="text/plain"
        )
        == "auto_generated"
    )
    assert (
        mime.classify(
            {}, from_address="maria@x.example", subject="Извън офиса до понеделник", content_type="text/plain"
        )
        == "out_of_office"
    )
    assert (
        mime.classify({}, from_address="maria@x.example", subject="Re: offer", content_type="text/plain") == "message"
    )


def test_outbound_messages_are_built_safely() -> None:
    raw = mime.build_message(
        sender=MAILBOX,
        sender_name="SEWEB",
        to=PROSPECT,
        subject="Онлайн резервации за Салон Аврора",
        body="Здравейте!\n\n--\nНепоискано търговско съобщение",
        message_id="<abc@seweb.example>",
        in_reply_to="<their@mail.example>",
        references=["<first@mail.example>"],
        unsubscribe_url="https://crm.example/api/v1/unsubscribe/token",
        unsubscribe_mailto="unsubscribe@seweb.example",
    )
    from email import message_from_bytes

    parsed = message_from_bytes(raw)
    assert (
        parsed["To"] == PROSPECT
        and parsed["Message-ID"] == "<abc@seweb.example>"
        and parsed["In-Reply-To"] == "<their@mail.example>"
    )
    assert parsed["References"] == "<first@mail.example> <their@mail.example>"
    assert (
        " ".join(parsed["List-Unsubscribe"].split())  # long headers are folded on the wire
        == "<https://crm.example/api/v1/unsubscribe/token>, <mailto:unsubscribe@seweb.example>"
    )
    assert parsed["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "Здравейте!" in parsed.get_payload(decode=True).decode()  # type: ignore[union-attr]
    for field in ("to", "subject"):
        with pytest.raises(ValueError, match="line break"):
            mime.build_message(
                **{
                    "sender": MAILBOX,
                    "sender_name": None,
                    "to": PROSPECT,
                    "subject": "s",
                    "body": "b",
                    "message_id": "<x@y>",
                    field: "value\r\nBcc: attacker@evil.example",
                }
            )


def test_gmail_provider_contract() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path, token = request.url.path, request.headers["Authorization"]
        if token == "Bearer expired":
            return httpx.Response(401)
        if token == "Bearer limited":
            return httpx.Response(429, headers={"Retry-After": "90"})
        if path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "Sales@Seweb.Example", "historyId": "4242"})
        if path.endswith("/watch"):
            return httpx.Response(200, json={"historyId": "4250", "expiration": "1792000000000"})
        if path.endswith("/history"):
            if request.url.params["startHistoryId"] == "1":
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "historyId": "4300",
                    "nextPageToken": "p2",
                    "history": [
                        {"id": "4260", "messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}]},
                        {
                            "id": "4270",
                            "messagesAdded": [
                                {"message": {"id": "m2", "threadId": "t1"}},
                                {"message": {"id": "m1", "threadId": "t1"}},
                            ],
                        },
                    ],
                },
            )
        if path.endswith("/messages/send"):
            if json.loads(request.content).get("threadId") == "slow":
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(200, json={"id": "sent-1", "threadId": "t9", "labelIds": ["SENT"]})
        if path.endswith("/messages/m1"):
            text_part = base64.urlsafe_b64encode("Здравейте".encode()).decode().rstrip("=")
            return httpx.Response(
                200,
                json={
                    "id": "m1",
                    "threadId": "t1",
                    "labelIds": ["INBOX"],
                    "internalDate": "1791460000000",
                    "snippet": "Здр",
                    "payload": {
                        "mimeType": "multipart/mixed",
                        "headers": [{"name": "From", "value": "a@b.example"}, {"name": "Subject", "value": "Hi"}],
                        "parts": [
                            {"mimeType": "text/plain", "body": {"data": text_part}},
                            {
                                "mimeType": "application/pdf",
                                "filename": "offer.pdf",
                                "body": {"attachmentId": "A1", "size": 9000},
                            },
                        ],
                    },
                },
            )
        if path.endswith("/messages/gone"):
            return httpx.Response(404)
        if path.endswith("/messages"):
            if "rfc822msgid" in request.url.params.get("q", ""):
                return httpx.Response(200, json={"messages": [{"id": "sent-1", "threadId": "t9"}]})
            return httpx.Response(200, json={"messages": [{"id": "m1", "threadId": "t1"}], "nextPageToken": "next"})
        return httpx.Response(400)

    provider = GmailProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    assert provider.profile("ok") == ("sales@seweb.example", "4242")
    history_id, expires = provider.watch("ok", "projects/p/topics/gmail")
    assert history_id == "4250" and expires == datetime.fromtimestamp(1792000000, tz=UTC)
    assert json.loads(calls[-1].content)["topicName"] == "projects/p/topics/gmail"
    page = provider.list_history("ok", "4200", None)
    assert (page.message_ids, page.max_record_id, page.mailbox_history_id, page.next_page_token) == (
        ["m1", "m2"],
        "4270",
        "4300",
        "p2",
    )
    assert calls[-1].url.params["historyTypes"] == "messageAdded"
    with pytest.raises(HistoryExpired):
        provider.list_history("ok", "1", None)
    assert provider.list_messages("ok", "newer_than:30d", None) == (["m1"], "next")
    message = provider.get_message("ok", "m1")
    assert message is not None and message.text == "Здравейте" and message.headers["from"] == "a@b.example"
    assert message.attachments == [{"filename": "offer.pdf", "mime_type": "application/pdf", "size": 9000}]
    assert provider.get_message("ok", "gone") is None
    assert not any("attachments" in str(c.url) for c in calls)  # attachment bodies are never requested
    assert provider.send("ok", b"raw message", "t9") == ("sent-1", "t9")
    assert base64.urlsafe_b64decode(json.loads(calls[-1].content)["raw"] + "==") == b"raw message"
    with pytest.raises(MailboxError) as slow:
        provider.send("ok", b"raw", "slow")
    assert slow.value.ambiguous is True  # a timeout on send may still have gone out
    assert provider.find_by_rfc_id("ok", "<abc@seweb.example>") == ("sent-1", "t9")
    assert calls[-1].url.params["q"] == "rfc822msgid:abc@seweb.example"
    with pytest.raises(MailboxError) as expired:
        provider.profile("expired")
    assert expired.value.revoked is True
    with pytest.raises(MailboxError) as limited:
        provider.profile("limited")
    assert limited.value.rate_limited is True and limited.value.retry_after == 90
    assert all(c.headers["Authorization"].startswith("Bearer ") and "ok" not in str(c.url) for c in calls)


def test_email_data_stays_inside_the_tenant(owner: TestClient, gmail: dict[str, Any], make_client: Any) -> None:
    box: FakeMailbox = gmail["box"]
    lead = lead_with_email(owner)
    mailbox = connected(owner, gmail)
    box.add(sender=PROSPECT, subject="Hello")
    run_sync(gmail)
    thread = ok(owner.get(f"{API}/email-threads", params={"lead_id": lead["id"]}))["items"][0]
    draft = ok(owner.post(f"{API}/email-drafts", json={"lead_id": lead["id"]}), 201)
    suppression = ok(owner.post(f"{API}/email-suppressions", json={"value": "blocked@example.bg"}), 201)
    other = make_client()
    sign_in(other, "other@example.test")
    create_workspace(other, "SEWEB")
    assert ok(other.get(f"{API}/mailboxes")) == [] and ok(other.get(f"{API}/email-suppressions"))["total"] == 0
    assert ok(other.get(f"{API}/email-threads", params={"needs_review": True}))["total"] == 0
    assert ok(other.get(f"{API}/email-drafts")) == []
    for method, path, body in (
        ("GET", f"/email-drafts/{draft['id']}", None),
        ("PATCH", f"/email-drafts/{draft['id']}", {"subject": "hijacked"}),
        ("POST", f"/email-threads/{thread['id']}/link", {"ignore": True}),
        ("POST", f"/mailboxes/{mailbox['id']}/sync", None),
        ("DELETE", f"/mailboxes/{mailbox['id']}", None),
        ("POST", f"/email-suppressions/{suppression['id']}/lift", {"lift_note": "let me in please"}),
        ("POST", "/email-drafts", {"lead_id": lead["id"]}),
        ("POST", "/email-drafts", {"kind": "reply", "thread_id": thread["id"]}),
    ):
        assert other.request(method, f"{API}{path}", json=body).status_code in (404, 422), path
    # The other tenant's suppression list and policy are their own.
    assert ok(other.get(f"{API}/outreach-policy"))["status"] == "draft"
    viewer = make_client()
    sign_in(viewer, "viewer@example.test")
    join(owner, viewer, "viewer@example.test", "read_only")
    assert viewer.post(f"{API}/email-drafts", json={"lead_id": lead["id"]}).status_code == 403
    assert viewer.post(f"{API}/email-suppressions", json={"value": "x@example.bg"}).status_code == 403
    assert viewer.get(f"{API}/email-threads", params={"lead_id": lead["id"]}).status_code == 200
