import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppRoutes, createQueryClient } from "../App";
import { json, makeMe, makeProspect, mockApi } from "../test/mockApi";

const TENANT = { id: "tenant-a", name: "SEWEB" };
const me = (permissions: string[]) => () => json(makeMe({ active: { ...TENANT, permissions: ["crm.read", ...permissions] } }));
const page = <T,>(items: T[]) => ({ items, total: items.length, limit: 50, offset: 0 });

function renderAt(path: string) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function message(overrides: Record<string, unknown> = {}) {
  return {
    id: crypto.randomUUID(),
    direction: "inbound",
    classification: "reply",
    from_address: "office@example-salon.bg",
    to_addresses: ["sales@seweb.example"],
    subject: "Re: Online booking",
    snippet: "Interested",
    body_text: "We are interested.",
    body_html: '<p onclick="steal()">We are interested.</p><img src="https://tracker.example/p.gif">',
    has_remote_content: true,
    attachments: [{ filename: "menu.pdf", mime_type: "application/pdf", size: 1200 }],
    sent_at: "2026-10-20T09:00:00Z",
    ...overrides,
  };
}

function thread(overrides: Record<string, unknown> = {}) {
  return {
    id: "th-1",
    mailbox_id: "mb-1",
    subject: "Online booking",
    company_id: "c1",
    lead_id: "lead-1",
    link_state: "linked",
    link_note: null,
    last_message_at: "2026-10-20T09:00:00Z",
    has_inbound: true,
    messages: [message()],
    ...overrides,
  };
}

function draft(overrides: Record<string, unknown> = {}) {
  return {
    id: "d-1",
    kind: "unsolicited",
    lead_id: "lead-1",
    company_id: "c1",
    thread_id: null,
    mailbox_id: null,
    to_address: "office@example-salon.bg",
    subject: "Online booking for Salon Aurora",
    body_text: "Hello,",
    version: 1,
    status: "draft",
    approved_version: null,
    approved_at: null,
    created_at: "2026-10-20T08:00:00Z",
    eligibility: {
      outcome: "block",
      reasons: [
        { level: "block", code: "policy_not_approved", message: "The outreach rules for this workspace have not been approved by the owner." },
        { level: "review", code: "recipient_unclassified", message: "It is not recorded whether this address belongs to a company or a private person." },
      ],
      checks: {},
      policy_version: "bg-2026-10-draft",
      rendered_body: "Hello,\n\n--\nUnsolicited commercial message\nSEWEB Ltd, Sofia",
    },
    ...overrides,
  };
}

const mailbox = {
  id: "mb-1",
  provider: "gmail",
  email_address: "sales@seweb.example",
  mode: "internal",
  status: "degraded",
  verification: "implemented",
  scopes: [],
  last_synced_at: "2026-10-20T09:00:00Z",
  last_notification_at: null,
  watch_expires_at: null,
  full_sync_required: false,
  last_error: null,
  healthy: false,
  problems: ["Push notifications could not be renewed; replies are still collected every 15 minutes."],
  can_read_replies: true,
};

afterEach(() => vi.restoreAllMocks());

describe("prospect email", () => {
  const prospect = { ...makeProspect(), listing_url: null, listing_id_type: "unknown", scores: [], calls: [], channels: [] };

  it("shows why a draft may not be sent, previews the final text and never offers to send", async () => {
    let current = draft();
    const calls = mockApi({
      "GET /api/v1/auth/me": me(["outreach.draft", "research.review"]),
      "GET /api/v1/prospects/lead-1": () => json(prospect),
      "GET /api/v1/email-threads": () => json(page([thread()])),
      "GET /api/v1/email-drafts": () => json([current]),
      "PATCH /api/v1/email-drafts/d-1": (call) => {
        current = draft({ ...(call.body as object), version: 2 });
        return json(current);
      },
      "PUT /api/v1/recipient-profiles": () => json({}),
    });
    renderAt("/prospects/lead-1");

    const editor = await screen.findByRole("article", { name: "Draft to office@example-salon.bg" });
    expect(within(editor).getByText("May not be sent")).toBeInTheDocument();
    expect(within(editor).getByText(/have not been approved by the owner/)).toBeInTheDocument();
    expect(within(editor).getByText(/Unsolicited commercial message/)).toBeInTheDocument(); // the label is in the preview
    expect(screen.queryByRole("button", { name: /send/i })).not.toBeInTheDocument();
    expect(within(editor).getByText(/Sending from the application is not switched on yet/)).toBeInTheDocument();

    // The conversation is shown as plain text: markup from the mailbox is never put into the page.
    expect(screen.getByText("We are interested.")).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
    expect(document.querySelector("[onclick]")).toBeNull();
    expect(screen.getByText(/Attachments stay in the mailbox.*menu\.pdf/)).toBeInTheDocument();

    const body = within(editor).getByLabelText("Message");
    await userEvent.clear(body);
    await userEvent.type(body, "Shorter text");
    expect(within(editor).getByText(/save to check your changes/)).toBeInTheDocument();
    await userEvent.click(within(editor).getByRole("button", { name: "Save and re-check" }));
    await waitFor(() => expect(calls.some((call) => call.method === "PATCH")).toBe(true));
    expect(calls.find((call) => call.method === "PATCH")?.body).toMatchObject({ body_text: "Shorter text" });

    const updated = await screen.findByRole("article", { name: "Draft to office@example-salon.bg" });
    const classify = within(updated).getByRole("form", { name: "Record who office@example-salon.bg belongs to" });
    await userEvent.type(within(classify).getByLabelText("What this is based on"), "Company register entry");
    await userEvent.click(within(classify).getByRole("button", { name: "Record" }));
    await waitFor(() => expect(calls.some((call) => call.method === "PUT")).toBe(true));
    expect(calls.find((call) => call.method === "PUT")?.body).toEqual({
      address: "office@example-salon.bg",
      legal_form: "legal_person",
      context: "business",
      evidence: "Company register entry",
    });
  });

  it("gives a read-only member the conversation but no way to write", async () => {
    mockApi({
      "GET /api/v1/auth/me": me([]),
      "GET /api/v1/prospects/lead-1": () => json(prospect),
      "GET /api/v1/email-threads": () => json(page([thread()])),
      "GET /api/v1/email-drafts": () => json([]),
    });
    renderAt("/prospects/lead-1");
    expect(await screen.findByText("We are interested.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Draft a reply/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start a draft" })).not.toBeInTheDocument();
  });
});

describe("email page", () => {
  const policy = {
    id: "p-1",
    jurisdiction: "BG",
    version: "bg-2026-10-draft",
    status: "draft",
    rules: { label_text: "Непоискано търговско съобщение", register_max_age_days: 7 },
    source_checked_on: null,
    source_note: "Based on an unofficial consolidation; not legal advice.",
    approved_at: null,
    approval_note: null,
    sender_identity: null,
    opt_out_register: { state: "unavailable", note: "No opt-out register has been imported." },
  };

  it("shows mailbox problems, unmatched messages and the state of the rules", async () => {
    const unmatched = thread({ id: "th-2", lead_id: null, company_id: null, link_state: "conflict", link_note: "The sender address belongs to 2 companies.", subject: "Question" });
    const calls = mockApi({
      "GET /api/v1/auth/me": me(["crm.write", "outreach.draft"]),
      "GET /api/v1/mailboxes": () => json([mailbox]),
      "GET /api/v1/email-threads": () => json(page([unmatched])),
      "GET /api/v1/outreach-policy": () => json(policy),
      "GET /api/v1/email-suppressions": () =>
        json(page([{ id: "s-1", scope: "address", value: "gone@example.bg", reason: "permanent_bounce", source: "mailbox", note: null, created_at: "2026-10-19T09:00:00Z", lifted_at: null, lift_note: null }])),
      "GET /api/v1/leads": () => json(page([{ id: "lead-9", company_name: "Vet Clinic Two" }])),
      "POST /api/v1/email-threads/th-2/link": () => json({ ...unmatched, link_state: "linked", lead_id: "lead-9" }),
    });
    renderAt("/email");

    expect(await screen.findByText(/Push notifications could not be renewed/)).toBeInTheDocument();
    expect(await screen.findByText("The sender address belongs to 2 companies.")).toBeInTheDocument();
    expect(await screen.findByText(/are a draft\. Email that the recipient has not asked for stays blocked/)).toBeInTheDocument();
    expect(screen.getByText("None imported. A missing register is never treated as empty.")).toBeInTheDocument();
    expect(await screen.findByText("gone@example.bg")).toBeInTheDocument();
    // Without the right permissions the approval and settings forms are not offered.
    expect(screen.queryByRole("form", { name: "Approve the outreach rules" })).not.toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Sender identification" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Lift/ })).not.toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Prospect name"), "Vet");
    await userEvent.click(screen.getByRole("button", { name: "Find" }));
    await userEvent.click(await screen.findByRole("button", { name: "Match to Vet Clinic Two" }));
    await waitFor(() => expect(calls.some((call) => call.method === "POST")).toBe(true));
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({ lead_id: "lead-9" });
  });

  it("says plainly when no mailbox is connected and lets the owner approve the rules", async () => {
    const calls = mockApi({
      "GET /api/v1/auth/me": me(["tenant.settings", "owners.manage"]),
      "GET /api/v1/mailboxes": () => json([]),
      "GET /api/v1/email-threads": () => json(page([])),
      "GET /api/v1/outreach-policy": () => json({ ...policy, sender_identity: "SEWEB Ltd, Sofia, Bulgaria" }),
      "GET /api/v1/email-suppressions": () => json(page([])),
      "POST /api/v1/outreach-policy/approve": () => json({ ...policy, status: "approved", version: "bg-2026-10-20-1", approved_at: "2026-10-20T10:00:00Z" }),
    });
    renderAt("/email");
    expect(await screen.findByText(/No mailbox is connected, so replies are not being read/)).toBeInTheDocument();
    expect(await screen.findByText("Nothing waiting to be matched.")).toBeInTheDocument();
    const form = await screen.findByRole("form", { name: "Approve the outreach rules", hidden: true });
    await userEvent.click(screen.getByText("Approve the rules"));
    await userEvent.type(within(form).getByLabelText(/Who reviewed the current law/), "Reviewed by counsel on 20 October 2026");
    await userEvent.type(within(form).getByLabelText("Date the legal source was checked"), "2026-10-20");
    await userEvent.click(within(form).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(calls.some((call) => call.method === "POST")).toBe(true));
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({
      approval_note: "Reviewed by counsel on 20 October 2026",
      source_checked_on: "2026-10-20",
      label_text: "Непоискано търговско съобщение",
      register_max_age_days: 7,
    });
  });
});

describe("mailbox on the integrations page", () => {
  it("explains why Gmail cannot be connected instead of offering a dead button", async () => {
    mockApi({
      "GET /api/v1/auth/me": me(["integrations.manage", "reports.read"]),
      "GET /api/v1/mailboxes": () => json([]),
      "GET /api/v1/mailboxes/gmail/availability": () =>
        json({ available: false, reason: "Gmail is not configured on this installation.", internal_domain: null }),
      "GET /api/v1/provider-adapters": () => json([]),
      "GET /api/v1/provider-connections": () => json([]),
      "GET /api/v1/budgets": () => json([]),
      "GET /api/v1/usage/summary": () =>
        json({ currency: "EUR", period_start: "2026-10-01", verified_amount: "0", estimated_amount: "0", unknown_reserved_amount: "0", reserved_amount: "0", by_provider: [], entries: 0 }),
    });
    renderAt("/integrations");
    expect(await screen.findByText("No mailbox connected.")).toBeInTheDocument();
    expect(await screen.findByText(/Gmail cannot be connected here: Gmail is not configured on this installation\./)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Connect a/ })).not.toBeInTheDocument();
  });
});
