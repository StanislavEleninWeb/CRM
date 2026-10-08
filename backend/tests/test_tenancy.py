"""Tenant isolation, roles, invitations and the database policies themselves."""

import os
from collections.abc import Iterator
from itertools import pairwise
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.db import RLS_KEY, RlsContext, session_scope
from app.core.deps import UserSession
from app.core.errors import PermissionDeniedError, register_error_handlers
from app.core.security import hash_token
from app.modules.identity.permissions import Permission, Role, can_assign_role, permissions_for
from app.worker.context import tenant_job
from tests.helpers import API, create_workspace, invite, join, sign_in

OWNER, ADMIN, MANAGER = "owner@example.test", "admin@example.test", "manager@example.test"
REP, VIEWER, OTHER = "rep@example.test", "viewer@example.test", "other@example.test"


@pytest.fixture
def two_tenants(make_client: Any) -> tuple[TestClient, str, TestClient, str]:
    """Two unrelated workspaces that deliberately share the same name."""
    alice, bob = make_client(), make_client()
    sign_in(alice, OWNER)
    sign_in(bob, OTHER)
    return alice, create_workspace(alice, "Acme"), bob, create_workspace(bob, "Acme")


def _user_id(client: TestClient) -> str:
    return str(client.get(f"{API}/auth/me").json()["user"]["id"])


# --- onboarding ------------------------------------------------------------------------------


def test_creating_a_workspace_makes_the_user_its_owner(client: TestClient) -> None:
    sign_in(client, OWNER)
    assert client.get(f"{API}/members").status_code == 403  # no workspace selected yet
    tenant_id = create_workspace(client, "  SEWEB  ")
    me = client.get(f"{API}/auth/me").json()
    assert me["active_tenant"]["id"] == tenant_id
    assert me["active_tenant"]["name"] == "SEWEB"
    assert me["active_tenant"]["role"] == "owner"
    assert me["active_tenant"]["currency"] == "EUR"
    assert me["active_tenant"]["timezone"] == "Europe/Sofia"
    assert "owners.manage" in me["active_tenant"]["permissions"]
    members = client.get(f"{API}/members").json()
    assert [m["email"] for m in members["items"]] == [OWNER]
    actions = [e["action"] for e in client.get(f"{API}/audit-events").json()["items"]]
    assert actions == ["tenant.created"]


def test_blank_workspace_name_is_rejected(client: TestClient) -> None:
    sign_in(client, OWNER)
    response = client.post(f"{API}/tenants", json={"name": "   "})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# --- isolation through the API ---------------------------------------------------------------


def test_same_named_workspaces_do_not_see_each_other(
    two_tenants: tuple[TestClient, str, TestClient, str],
) -> None:
    alice, tenant_a, bob, tenant_b = two_tenants
    assert tenant_a != tenant_b
    invite(alice, "new-a@example.test", "representative")
    invite(bob, "new-b@example.test", "representative")

    assert [m["email"] for m in alice.get(f"{API}/members").json()["items"]] == [OWNER]
    assert [m["email"] for m in bob.get(f"{API}/members").json()["items"]] == [OTHER]
    assert [i["email"] for i in alice.get(f"{API}/invitations").json()["items"]] == [
        "new-a@example.test"
    ]
    assert [i["email"] for i in bob.get(f"{API}/invitations").json()["items"]] == [
        "new-b@example.test"
    ]
    assert alice.get(f"{API}/tenant").json()["id"] == tenant_a
    assert [t["id"] for t in bob.get(f"{API}/auth/me").json()["tenants"]] == [tenant_b]
    for event in bob.get(f"{API}/audit-events").json()["items"]:
        assert event["actor_id"] == _user_id(bob)


def test_a_forged_tenant_header_changes_nothing(
    two_tenants: tuple[TestClient, str, TestClient, str],
) -> None:
    _alice, tenant_a, bob, tenant_b = two_tenants
    forged = {"X-Tenant-ID": tenant_a, "X-Tenant": tenant_a, "Tenant-Id": tenant_a}
    assert bob.get(f"{API}/tenant", headers=forged).json()["id"] == tenant_b
    assert [m["email"] for m in bob.get(f"{API}/members", headers=forged).json()["items"]] == [
        OTHER
    ]
    assert bob.get(f"{API}/members", params={"tenant_id": tenant_a}).json()["total"] == 1


def test_switching_to_a_workspace_you_do_not_belong_to_is_refused(
    two_tenants: tuple[TestClient, str, TestClient, str],
) -> None:
    _alice, tenant_a, bob, tenant_b = two_tenants
    assert bob.post(f"{API}/auth/switch-tenant", json={"tenant_id": tenant_a}).status_code == 403
    missing = bob.post(f"{API}/auth/switch-tenant", json={"tenant_id": str(uuid4())})
    assert missing.status_code == 403  # indistinguishable from "exists but not yours"
    assert bob.get(f"{API}/tenant").json()["id"] == tenant_b


def test_cross_tenant_writes_fail(two_tenants: tuple[TestClient, str, TestClient, str]) -> None:
    alice, _tenant_a, bob, _tenant_b = two_tenants
    invitation_a = invite(alice, "new-a@example.test", "representative")
    assert bob.delete(f"{API}/invitations/{invitation_a['id']}").status_code == 404
    assert (
        bob.patch(f"{API}/members/{_user_id(alice)}", json={"role": "read_only"}).status_code == 404
    )
    assert bob.delete(f"{API}/members/{_user_id(alice)}").status_code == 404
    assert alice.get(f"{API}/invitations").json()["items"][0]["status"] == "pending"
    assert alice.get(f"{API}/members").json()["items"][0]["role"] == "owner"


def test_member_of_two_workspaces_sees_only_the_active_one(
    two_tenants: tuple[TestClient, str, TestClient, str], make_client: Any
) -> None:
    alice, tenant_a, bob, tenant_b = two_tenants
    rep = make_client()
    sign_in(rep, REP)
    join(alice, rep, REP, "representative")
    join(bob, rep, REP, "read_only")
    assert rep.get(f"{API}/tenant").json()["id"] == tenant_b  # accepting switches to it
    assert rep.get(f"{API}/auth/me").json()["active_tenant"]["role"] == "read_only"
    assert sorted(m["email"] for m in rep.get(f"{API}/members").json()["items"]) == [OTHER, REP]
    assert rep.post(f"{API}/auth/switch-tenant", json={"tenant_id": tenant_a}).status_code == 204
    assert rep.get(f"{API}/auth/me").json()["active_tenant"]["role"] == "representative"
    assert sorted(m["email"] for m in rep.get(f"{API}/members").json()["items"]) == [OWNER, REP]


# --- the policies themselves, as the runtime role --------------------------------------------


@pytest.fixture
def app_engine() -> Iterator[Any]:
    engine = create_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    yield engine
    engine.dispose()


def _ctx(conn: Any, *, user: str | None = None, tenant: str | None = None) -> None:
    conn.execute(
        text("SELECT set_config('app.user_id', :u, true), set_config('app.tenant_id', :t, true)"),
        {"u": user or "", "t": tenant or ""},
    )


def test_without_context_nothing_is_visible_or_writable(
    two_tenants: tuple[TestClient, str, TestClient, str], app_engine: Any
) -> None:
    _alice, tenant_a, _bob, _tenant_b = two_tenants
    with app_engine.connect() as conn:
        assert conn.execute(text("SELECT current_user")).scalar() == "crm_app"
        for table in ("users", "tenants", "memberships", "invitations", "sessions", "audit_events"):
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0, table
        with pytest.raises(DBAPIError, match="row-level security"):
            conn.execute(
                text(
                    "INSERT INTO audit_events (tenant_id, actor_type, action, target_type) "
                    "VALUES (:t, 'system', 'x', 'y')"
                ),
                {"t": tenant_a},
            )


def test_policies_block_cross_tenant_reads_writes_and_links(
    two_tenants: tuple[TestClient, str, TestClient, str], app_engine: Any
) -> None:
    alice, tenant_a, bob, tenant_b = two_tenants
    invite(alice, "new-a@example.test", "representative")
    user_a, user_b = _user_id(alice), _user_id(bob)

    def as_bob(sql: str, **params: Any) -> Any:
        with app_engine.begin() as conn:
            _ctx(conn, user=user_b, tenant=tenant_b)
            return conn.execute(text(sql), params)

    # Reads: tenant A's rows do not exist as far as tenant B is concerned.
    with app_engine.begin() as conn:
        _ctx(conn, user=user_b, tenant=tenant_b)
        assert conn.execute(text("SELECT count(*) FROM invitations")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM memberships")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM tenants")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM users")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM audit_events")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM sessions")).scalar() == 1

    # Writes into another tenant are rejected by the policy.
    for statement in (
        "INSERT INTO invitations (tenant_id, email, role, token_hash, expires_at) "
        "VALUES (:a, 'x@example.test', 'owner', '\\x01', now() + interval '1 day')",
        "INSERT INTO memberships (tenant_id, user_id, role) VALUES (:a, :ub, 'owner')",
        "INSERT INTO audit_events (tenant_id, actor_type, action, target_type) "
        "VALUES (:a, 'system', 'x', 'y')",
    ):
        with pytest.raises(DBAPIError, match="row-level security"):
            as_bob(statement, a=tenant_a, ub=user_b)

    # Updates and deletes aimed at another tenant match no rows.
    assert (
        as_bob(
            "UPDATE memberships SET role = 'read_only' WHERE tenant_id = :a", a=tenant_a
        ).rowcount
        == 0
    )
    assert as_bob("DELETE FROM invitations WHERE tenant_id = :a", a=tenant_a).rowcount == 0
    assert as_bob("UPDATE tenants SET name = 'hacked' WHERE id = :a", a=tenant_a).rowcount == 0

    # A row cannot be moved into another tenant.
    as_bob(
        "INSERT INTO invitations (tenant_id, email, role, token_hash, invited_by, expires_at) "
        "VALUES (:b, 'mine@example.test', 'read_only', '\\x02', :ub, now() + interval '1 day')",
        b=tenant_b,
        ub=user_b,
    )
    with pytest.raises(DBAPIError, match="row-level security"):
        as_bob("UPDATE invitations SET tenant_id = :a WHERE tenant_id = :b", a=tenant_a, b=tenant_b)

    # Links: a tenant-aware foreign key refuses a reference to another tenant's member.
    with pytest.raises(DBAPIError, match="fk_invitations_inviter"):
        as_bob(
            "INSERT INTO invitations (tenant_id, email, role, token_hash, invited_by, expires_at) "
            "VALUES (:b, 'link@example.test', 'read_only', '\\x03', :ua, now() + interval '1 day')",
            b=tenant_b,
            ua=user_a,
        )

    # The runtime role cannot make itself a platform operator or rewrite audit history.
    with pytest.raises(DBAPIError, match="permission denied"):
        as_bob("UPDATE users SET is_platform_operator = true WHERE id = :ub", ub=user_b)
    with pytest.raises(DBAPIError, match="permission denied"):
        as_bob("DELETE FROM audit_events")
    with pytest.raises(DBAPIError, match="permission denied"):
        as_bob("UPDATE audit_events SET action = 'rewritten'")
    with pytest.raises(DBAPIError, match="permission denied"):
        as_bob("INSERT INTO tenants (name) VALUES ('direct')")


def test_tenant_context_does_not_leak_through_the_connection_pool(
    two_tenants: tuple[TestClient, str, TestClient, str], app_engine: Any
) -> None:
    alice, tenant_a, _bob, _tenant_b = two_tenants
    user_a = _user_id(alice)

    def backend_pid(conn: Any) -> int:
        return int(conn.execute(text("SELECT pg_backend_pid()")).scalar())

    with pytest.raises(RuntimeError), app_engine.begin() as conn:
        _ctx(conn, user=user_a, tenant=tenant_a)
        first_pid = backend_pid(conn)
        assert conn.execute(text("SELECT count(*) FROM memberships")).scalar() == 1
        raise RuntimeError("request failed mid-transaction")

    with app_engine.connect() as conn:
        assert backend_pid(conn) == first_pid  # the very same pooled connection
        assert conn.execute(text("SELECT app_current_tenant()")).scalar() is None
        assert conn.execute(text("SELECT app_current_user()")).scalar() is None
        assert conn.execute(text("SELECT count(*) FROM memberships")).scalar() == 0

    with app_engine.begin() as conn:  # and after a clean commit
        _ctx(conn, user=user_a, tenant=tenant_a)
    with app_engine.connect() as conn:
        assert conn.execute(text("SELECT app_current_tenant()")).scalar() is None


def test_context_survives_a_commit_in_the_middle_of_a_unit_of_work(
    two_tenants: tuple[TestClient, str, TestClient, str],
) -> None:
    alice, tenant_a, _bob, _tenant_b = two_tenants
    context = RlsContext(user_id=UUID(_user_id(alice)), tenant_id=UUID(tenant_a))
    with session_scope(context) as session:
        assert session.execute(text("SELECT count(*) FROM memberships")).scalar() == 1
        session.commit()
        assert session.execute(text("SELECT count(*) FROM memberships")).scalar() == 1
        session.rollback()
        assert session.execute(text("SELECT app_current_tenant()")).scalar() == UUID(tenant_a)
    with session_scope() as session:
        assert RLS_KEY not in session.info
        assert session.execute(text("SELECT count(*) FROM memberships")).scalar() == 0


def test_definer_functions_cannot_be_used_to_enumerate(
    two_tenants: tuple[TestClient, str, TestClient, str], app_engine: Any
) -> None:
    alice, _tenant_a, bob, _tenant_b = two_tenants
    token = invite(alice, "new-a@example.test", "representative")["token"]
    with app_engine.begin() as conn:
        _ctx(conn, user=_user_id(bob))
        assert (
            conn.execute(
                text("SELECT count(*) FROM invitation_peek(:h)"), {"h": b"\x00" * 32}
            ).scalar()
            == 0
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM auth_lookup_session(:h)"), {"h": b"\x00" * 32}
            ).scalar()
            == 0
        )
        peek = conn.execute(
            text("SELECT * FROM invitation_peek(:h)"), {"h": hash_token(token)}
        ).one()
        assert set(peek._mapping) == {"tenant_name", "email", "role", "status"}  # no identifiers
    with app_engine.begin() as conn, pytest.raises(DBAPIError, match="authenticated user"):
        conn.execute(text("SELECT tenant_create('anonymous')"))  # no user context


# --- invitations -----------------------------------------------------------------------------


def test_invitation_token_is_shown_once_and_stored_hashed(
    two_tenants: tuple[TestClient, str, TestClient, str], migrator_engine: Any
) -> None:
    alice, *_ = two_tenants
    created = invite(alice, "New.Person@Example.Test", "representative")
    assert created["email"] == "new.person@example.test"
    assert created["accept_url"].endswith(f"/invitations/accept?token={created['token']}")
    listed = alice.get(f"{API}/invitations").json()["items"][0]
    assert "token" not in listed
    with migrator_engine.connect() as conn:
        stored = bytes(conn.execute(text("SELECT token_hash FROM invitations")).scalar_one())
    assert stored == hash_token(created["token"]) and created["token"].encode() not in stored


def test_accepting_an_invitation(
    two_tenants: tuple[TestClient, str, TestClient, str], make_client: Any
) -> None:
    alice, tenant_a, _bob, _tenant_b = two_tenants
    token = invite(alice, REP, "representative")["token"]
    peek = make_client().get(f"{API}/invitations/lookup", params={"token": token}).json()
    assert peek == {
        "tenant_name": "Acme",
        "email": REP,
        "role": "representative",
        "status": "pending",
    }

    rep = make_client()
    sign_in(rep, REP)
    assert rep.post(f"{API}/invitations/accept", json={"token": token}).json()["id"] == tenant_a
    assert rep.get(f"{API}/auth/me").json()["active_tenant"]["role"] == "representative"
    # Single use.
    assert rep.post(f"{API}/invitations/accept", json={"token": token}).status_code == 409
    assert alice.get(f"{API}/invitations").json()["items"][0]["status"] == "accepted"
    assert "invitation.accepted" in [
        e["action"] for e in alice.get(f"{API}/audit-events").json()["items"]
    ]


def test_invitation_for_a_different_email_is_refused(
    two_tenants: tuple[TestClient, str, TestClient, str], make_client: Any
) -> None:
    alice, *_ = two_tenants
    token = invite(alice, REP, "administrator")["token"]
    viewer = make_client()
    sign_in(viewer, VIEWER)
    assert viewer.post(f"{API}/invitations/accept", json={"token": token}).status_code == 403
    assert viewer.get(f"{API}/auth/me").json()["tenants"] == []


def test_expired_revoked_and_unknown_invitations_fail(
    two_tenants: tuple[TestClient, str, TestClient, str], make_client: Any, migrator_engine: Any
) -> None:
    alice, *_ = two_tenants
    rep = make_client()
    sign_in(rep, REP)

    expired = invite(alice, REP, "representative")
    with migrator_engine.begin() as conn:
        conn.execute(text("UPDATE invitations SET expires_at = now() - interval '1 minute'"))
    assert (
        rep.post(f"{API}/invitations/accept", json={"token": expired["token"]}).status_code == 409
    )

    revoked = invite(alice, REP, "representative")
    assert alice.delete(f"{API}/invitations/{revoked['id']}").status_code == 204
    assert (
        rep.post(f"{API}/invitations/accept", json={"token": revoked["token"]}).status_code == 409
    )
    assert alice.delete(f"{API}/invitations/{revoked['id']}").status_code == 404

    assert rep.post(f"{API}/invitations/accept", json={"token": "z" * 43}).status_code == 404
    assert rep.get(f"{API}/invitations/lookup", params={"token": "z" * 43}).status_code == 404
    assert rep.get(f"{API}/auth/me").json()["tenants"] == []


def test_removed_member_loses_access_immediately(
    two_tenants: tuple[TestClient, str, TestClient, str], make_client: Any
) -> None:
    alice, *_ = two_tenants
    rep = make_client()
    sign_in(rep, REP)
    join(alice, rep, REP, "representative")
    assert rep.get(f"{API}/members").status_code == 200
    assert alice.delete(f"{API}/members/{_user_id(rep)}").status_code == 204
    assert rep.get(f"{API}/members").status_code == 403
    assert rep.get(f"{API}/tenant").status_code == 403
    assert rep.get(f"{API}/auth/me").json()["active_tenant"] is None


# --- roles -----------------------------------------------------------------------------------


@pytest.fixture
def team(make_client: Any) -> dict[str, TestClient]:
    clients: dict[str, TestClient] = {}
    owner = make_client()
    sign_in(owner, OWNER)
    create_workspace(owner, "SEWEB")
    clients["owner"] = owner
    for role, email in (
        ("administrator", ADMIN),
        ("sales_manager", MANAGER),
        ("representative", REP),
        ("read_only", VIEWER),
    ):
        member = make_client()
        sign_in(member, email)
        join(owner, member, email, role)
        clients[role] = member
    return clients


def test_permission_matrix_is_monotonic() -> None:
    order = [
        Role.READ_ONLY,
        Role.REPRESENTATIVE,
        Role.SALES_MANAGER,
        Role.ADMINISTRATOR,
        Role.OWNER,
    ]
    for lower, higher in pairwise(order):
        assert permissions_for(lower) < permissions_for(higher)
    assert Permission.CRM_WRITE not in permissions_for(Role.READ_ONLY)
    assert Permission.MEMBERS_MANAGE not in permissions_for(Role.SALES_MANAGER)
    assert Permission.OWNERS_MANAGE not in permissions_for(Role.ADMINISTRATOR)
    assert not can_assign_role(Role.ADMINISTRATOR, current=None, new=Role.OWNER)
    assert not can_assign_role(Role.ADMINISTRATOR, current=Role.OWNER, new=None)
    assert can_assign_role(Role.ADMINISTRATOR, current=Role.REPRESENTATIVE, new=Role.SALES_MANAGER)
    assert not can_assign_role(Role.SALES_MANAGER, current=Role.READ_ONLY, new=Role.REPRESENTATIVE)


def test_only_managers_of_members_can_invite_or_change_roles(team: dict[str, TestClient]) -> None:
    target = _user_id(team["read_only"])
    for role in ("read_only", "representative", "sales_manager"):
        member = team[role]
        body = {"email": "x@example.test", "role": "read_only"}
        assert member.post(f"{API}/invitations", json=body).status_code == 403, role
        assert member.get(f"{API}/invitations").status_code == 403, role
        assert member.patch(f"{API}/members/{target}", json={"role": "owner"}).status_code == 403, (
            role
        )
        assert member.delete(f"{API}/members/{target}").status_code == 403, role
        assert member.patch(f"{API}/tenant", json={"name": "Renamed"}).status_code == 403, role
        assert member.get(f"{API}/audit-events").status_code == 403, role
        assert member.get(f"{API}/members").status_code == 200, role
    assert team["owner"].get(f"{API}/tenant").json()["name"] == "SEWEB"


def test_a_member_cannot_escalate_their_own_role(team: dict[str, TestClient]) -> None:
    for role in ("read_only", "representative", "sales_manager", "administrator"):
        me = _user_id(team[role])
        assert team[role].patch(f"{API}/members/{me}", json={"role": "owner"}).status_code == 403, (
            role
        )
    roles = {m["email"]: m["role"] for m in team["owner"].get(f"{API}/members").json()["items"]}
    assert roles[ADMIN] == "administrator" and roles[VIEWER] == "read_only"


def test_administrator_manages_everyone_except_owners(team: dict[str, TestClient]) -> None:
    admin, owner_id = team["administrator"], _user_id(team["owner"])
    rep_id = _user_id(team["representative"])
    assert admin.patch(f"{API}/members/{rep_id}", json={"role": "sales_manager"}).status_code == 200
    assert admin.patch(f"{API}/members/{rep_id}", json={"role": "owner"}).status_code == 403
    assert admin.patch(f"{API}/members/{owner_id}", json={"role": "read_only"}).status_code == 403
    assert admin.delete(f"{API}/members/{owner_id}").status_code == 403
    assert (
        admin.post(
            f"{API}/invitations", json={"email": "o@example.test", "role": "owner"}
        ).status_code
        == 403
    )
    assert (
        admin.post(
            f"{API}/invitations", json={"email": "r@example.test", "role": "read_only"}
        ).status_code
        == 201
    )
    assert (
        admin.patch(f"{API}/tenant", json={"timezone": "Europe/Berlin"}).json()["timezone"]
        == "Europe/Berlin"
    )
    assert admin.patch(f"{API}/tenant", json={"timezone": "Mars/Olympus"}).status_code == 422
    events = admin.get(f"{API}/audit-events").json()["items"]
    changed = next(e for e in events if e["action"] == "member.role_changed")
    assert changed["data"] == {"from": "representative", "to": "sales_manager"}
    assert changed["actor_id"] == _user_id(admin) and changed["correlation_id"]


def test_read_only_members_cannot_mutate_anything(team: dict[str, TestClient]) -> None:
    viewer = team["read_only"]
    before = team["owner"].get(f"{API}/members").json()
    for method, path, body in (
        ("post", "/invitations", {"email": "x@example.test", "role": "read_only"}),
        ("patch", "/tenant", {"name": "X"}),
        ("patch", f"/members/{_user_id(viewer)}", {"role": "representative"}),
        ("delete", f"/members/{_user_id(team['representative'])}", None),
    ):
        response = viewer.request(method.upper(), f"{API}{path}", json=body)
        assert response.status_code == 403, path
    assert team["owner"].get(f"{API}/members").json() == before


def test_the_last_owner_cannot_be_removed_or_demoted(team: dict[str, TestClient]) -> None:
    owner = team["owner"]
    owner_id, admin_id = _user_id(owner), _user_id(team["administrator"])
    assert (
        owner.patch(f"{API}/members/{owner_id}", json={"role": "administrator"}).status_code == 409
    )
    assert owner.delete(f"{API}/members/{owner_id}").status_code == 409
    # With a second owner, the first may step down.
    assert owner.patch(f"{API}/members/{admin_id}", json={"role": "owner"}).status_code == 200
    assert (
        owner.patch(f"{API}/members/{owner_id}", json={"role": "administrator"}).status_code == 200
    )
    assert owner.patch(f"{API}/members/{admin_id}", json={"role": "read_only"}).status_code == 403
    new_owner = team["administrator"]
    assert new_owner.delete(f"{API}/members/{admin_id}").status_code == 409


def test_last_owner_rule_holds_at_database_level(
    two_tenants: tuple[TestClient, str, TestClient, str], app_engine: Any
) -> None:
    alice, tenant_a, _bob, _tenant_b = two_tenants
    with app_engine.begin() as conn, pytest.raises(DBAPIError, match="at least one owner"):
        _ctx(conn, user=_user_id(alice), tenant=tenant_a)
        conn.execute(text("DELETE FROM memberships"))


# --- transactions and background jobs --------------------------------------------------------


def test_a_failed_commit_is_reported_as_an_error_not_a_success() -> None:
    """The transaction commits before the response is sent."""
    from fastapi import APIRouter

    probe = FastAPI()
    register_error_handlers(probe)
    router = APIRouter()

    @router.post("/probe")
    def write_then_fail_commit(db: UserSession) -> dict[str, bool]:
        def failing_commit() -> None:
            raise RuntimeError("commit failed")

        db.commit = failing_commit  # type: ignore[method-assign]
        return {"written": True}

    probe.include_router(router)
    from app.core import deps

    principal = deps.Principal(
        user_id=uuid4(),
        session_id=uuid4(),
        active_tenant_id=None,
        csrf_token="t",
        mfa_claimed=False,
    )
    probe.dependency_overrides[deps.get_principal] = lambda: principal
    with TestClient(probe, raise_server_exceptions=False) as test_client:
        response = test_client.post("/probe")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"


def test_background_jobs_carry_and_recheck_tenant_context(
    two_tenants: tuple[TestClient, str, TestClient, str], make_client: Any
) -> None:
    alice, tenant_a, bob, tenant_b = two_tenants
    rep = make_client()
    sign_in(rep, REP)
    join(alice, rep, REP, "representative")
    rep_id = _user_id(rep)

    with tenant_job(tenant_a, rep_id) as session:
        assert isinstance(session, Session)
        assert session.execute(text("SELECT count(*) FROM memberships")).scalar() == 2
        assert session.execute(text("SELECT count(*) FROM tenants")).scalar() == 1
    with pytest.raises(PermissionDeniedError), tenant_job(None):
        pass
    with pytest.raises(PermissionDeniedError), tenant_job(str(uuid4())):
        pass
    with pytest.raises(PermissionDeniedError), tenant_job(tenant_b, rep_id):
        pass  # the actor is not a member of that tenant
    assert alice.delete(f"{API}/members/{rep_id}").status_code == 204
    with pytest.raises(PermissionDeniedError), tenant_job(tenant_a, rep_id):
        pass  # removed between enqueue and execution
    assert bob.get(f"{API}/members").json()["total"] == 1
