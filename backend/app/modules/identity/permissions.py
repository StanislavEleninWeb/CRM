"""Roles and the permission matrix.

Row-level security isolates tenants. This module decides what a member may do
inside their own tenant. Every mutating endpoint names the permission it needs.
"""

from enum import StrEnum


class Role(StrEnum):
    OWNER = "owner"
    ADMINISTRATOR = "administrator"
    SALES_MANAGER = "sales_manager"
    REPRESENTATIVE = "representative"
    READ_ONLY = "read_only"


class Permission(StrEnum):
    CRM_READ = "crm.read"
    CRM_WRITE = "crm.write"
    CRM_DELETE = "crm.delete"
    CRM_BULK = "crm.bulk"
    CRM_ASSIGN = "crm.assign"
    CRM_MERGE = "crm.merge"
    PIPELINE_MANAGE = "pipeline.manage"
    IMPORT_RUN = "import.run"
    EXPORT_RUN = "export.run"
    RESEARCH_REVIEW = "research.review"
    RESEARCH_RUN = "research.run"
    OUTREACH_DRAFT = "outreach.draft"
    OUTREACH_APPROVE = "outreach.approve"
    OUTREACH_SEND = "outreach.send"
    CALLS_LOG = "calls.log"
    REPORTS_READ = "reports.read"
    MEMBERS_READ = "members.read"
    MEMBERS_MANAGE = "members.manage"
    OWNERS_MANAGE = "owners.manage"
    TENANT_SETTINGS = "tenant.settings"
    TENANT_BILLING = "tenant.billing"
    INTEGRATIONS_MANAGE = "integrations.manage"
    AUDIT_READ = "audit.read"


P = Permission
_READ = {P.CRM_READ, P.REPORTS_READ, P.MEMBERS_READ}
_REPRESENTATIVE = _READ | {
    P.CRM_WRITE,
    P.RESEARCH_REVIEW,
    P.OUTREACH_DRAFT,
    P.OUTREACH_SEND,
    P.CALLS_LOG,
}
_MANAGER = _REPRESENTATIVE | {
    P.CRM_DELETE,
    P.CRM_BULK,
    P.CRM_ASSIGN,
    P.CRM_MERGE,
    P.PIPELINE_MANAGE,
    P.IMPORT_RUN,
    P.EXPORT_RUN,
    P.RESEARCH_RUN,
    P.OUTREACH_APPROVE,
}
_ADMIN = _MANAGER | {
    P.MEMBERS_MANAGE,
    P.TENANT_SETTINGS,
    P.INTEGRATIONS_MANAGE,
    P.AUDIT_READ,
}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.READ_ONLY: frozenset(_READ),
    Role.REPRESENTATIVE: frozenset(_REPRESENTATIVE),
    Role.SALES_MANAGER: frozenset(_MANAGER),
    Role.ADMINISTRATOR: frozenset(_ADMIN),
    Role.OWNER: frozenset(Permission),
}


def permissions_for(role: Role) -> frozenset[Permission]:
    return ROLE_PERMISSIONS[role]


def can_assign_role(actor: Role, *, current: Role | None, new: Role | None) -> bool:
    """Whether ``actor`` may move a member from ``current`` to ``new`` (None = no membership).

    Only owners may create, change or remove owners. Administrators manage everyone else.
    """
    if P.MEMBERS_MANAGE not in ROLE_PERMISSIONS[actor]:
        return False
    touches_owner = Role.OWNER in (current, new)
    return not touches_owner or P.OWNERS_MANAGE in ROLE_PERMISSIONS[actor]
