"""Which routes stay open in two special situations. Both are allow-lists: anything not named is closed.

Paths are route templates without the ``/api/v1`` prefix, as the router knows them.
"""

# A workspace whose subscription is not in good standing cannot create or send anything.
# It can always reduce access, stop contact, remove data, and reach billing: none of those
# may depend on having paid.
ALLOWED_WHEN_RESTRICTED: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/billing/checkout"),
        ("POST", "/billing/portal"),
        ("POST", "/billing/sync"),
        # taking access away
        ("DELETE", "/api-keys/{key_id}"),
        ("DELETE", "/support-grants/{grant_id}"),
        ("DELETE", "/members/{user_id}"),
        ("DELETE", "/invitations/{invitation_id}"),
        ("DELETE", "/mailboxes/{mailbox_id}"),
        ("DELETE", "/provider-connections/{connection_id}"),
        ("DELETE", "/webhook-endpoints/{endpoint_id}"),
        ("PATCH", "/webhook-endpoints/{endpoint_id}"),
        # stopping contact
        ("POST", "/email-suppressions"),
        ("POST", "/companies/{company_id}/restrictions"),
        ("POST", "/send-intents/{intent_id}/cancel"),
        ("DELETE", "/email-drafts/{draft_id}"),
        ("POST", "/research-runs/{run_id}/{action}"),
        # removing data
        ("POST", "/companies/{company_id}/erase"),
        ("DELETE", "/companies/{company_id}"),
        ("DELETE", "/contacts/{contact_id}"),
        ("DELETE", "/channels/{channel_id}"),
        ("DELETE", "/attachments/{attachment_id}"),
        ("POST", "/tenant/deletion"),
        ("DELETE", "/tenant/deletion"),
        ("PUT", "/retention"),
    }
)

# What a person with support access may read. Everything else, and every write, is refused.
SUPPORT_READABLE: frozenset[str] = frozenset(
    {
        "/tenant",
        "/entitlements",
        "/companies",
        "/companies/{company_id}",
        "/companies/{company_id}/duplicates",
        "/leads",
        "/leads/{lead_id}",
        "/leads/{lead_id}/scores",
        "/prospects",
        "/prospects/facets",
        "/prospects/{lead_id}",
        "/deals",
        "/deals/{deal_id}",
        "/deals/{deal_id}/stage-history",
        "/pipelines",
        "/tasks",
        "/tags",
        "/custom-fields",
        "/saved-views",
        "/call-queue",
        "/queue-settings",
        "/shortlists",
        "/shortlists/{shortlist_id}",
        "/rubric",
        "/research-configs",
        "/research-runs",
        "/research-runs/{run_id}",
        "/research-runs/{run_id}/queries",
        "/research-candidates",
        "/source-policies",
        "/reports/funnel",
        "/usage/summary",
        "/budgets",
    }
)

# Readable only when the owner ticked "include communications": anything that shows what was
# written to or by a contact, who was emailed, or free text people wrote about a conversation.
SUPPORT_COMMUNICATIONS: frozenset[str] = frozenset(
    {
        "/email-threads",
        "/email-drafts",
        "/email-drafts/{draft_id}",
        "/send-intents",
        "/email-suppressions",
        "/email-sending",
        "/mailboxes",
        "/outreach-policy",
        "/activities",
        "/notes",
        "/attachments",
        "/workspace/attention",
        "/reports/records",
    }
)


def support_may_read(route_path: str, include_communications: bool) -> bool:
    return route_path in SUPPORT_READABLE or (include_communications and route_path in SUPPORT_COMMUNICATIONS)
