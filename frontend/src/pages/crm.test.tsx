import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppRoutes, createQueryClient } from "../App";
import { json, makeMe, makeProspect, makeQueue, mockApi } from "../test/mockApi";

const TENANT = { id: "tenant-a", name: "SEWEB" };
const PERMISSIONS = ["crm.read", "crm.write", "members.read"];
const me = () => json(makeMe({ active: { ...TENANT, permissions: PERMISSIONS } }));

function renderAt(path: string) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function page<T>(items: T[], total = items.length) {
  return { items, total, limit: 50, offset: 0 };
}

function channel(overrides: Record<string, unknown>) {
  return {
    id: crypto.randomUUID(),
    company_id: "c1",
    contact_id: null,
    kind: "phone",
    purpose: "general",
    raw_value: "0887 000 003",
    normalized_value: "+359887000003",
    normalized_is_e164: true,
    label: null,
    source_type: "manual_entry",
    source_url: null,
    source_date: null,
    verification_state: "unverified",
    do_not_contact: false,
    restriction_reason: null,
    allow_sales_use: true,
    dial_uri: "tel:+359887000003",
    ...overrides,
  };
}

const companyDetail = {
  id: "c1",
  name: "Vet Clinic",
  external_id: null,
  business_type: "Veterinary clinic",
  industry_group: null,
  city: "Varna",
  country: "Bulgaria",
  language: null,
  website_url: null,
  domain: null,
  owner_user_id: null,
  source: "manual",
  next_action: null,
  next_action_at: null,
  merged_into_id: null,
  archived_at: null,
  custom: {},
  created_at: "2026-10-08T10:00:00Z",
  updated_at: "2026-10-08T10:00:00Z",
  contacts: [],
  restrictions: [],
  tags: [],
  open_task_count: 0,
  lead_count: 0,
  deal_count: 0,
  channels: [
    channel({}),
    channel({
      raw_value: "0884 000 004",
      purpose: "emergency",
      allow_sales_use: false,
      dial_uri: null,
    }),
    channel({
      raw_value: "0899 111 222",
      do_not_contact: true,
      restriction_reason: "Asked not to be called",
      allow_sales_use: false,
      dial_uri: null,
    }),
  ],
};

afterEach(() => vi.restoreAllMocks());

describe("company page", () => {
  it("offers a call link only for numbers that may be used for sales calls", async () => {
    mockApi({
      "GET /api/v1/auth/me": me,
      "GET /api/v1/companies/c1": () => json(companyDetail),
      "GET /api/v1/deals": () => json(page([])),
      "GET /api/v1/tasks": () => json(page([])),
      "GET /api/v1/activities": () => json(page([])),
    });
    renderAt("/companies/c1");
    const call = await screen.findByRole("link", { name: "Call 0887 000 003" });
    expect(call).toHaveAttribute("href", "tel:+359887000003");
    expect(screen.getAllByRole("link", { name: /^Call/ })).toHaveLength(1);
    expect(screen.getByText("Emergency line: not for sales calls")).toBeInTheDocument();
    expect(screen.getByText("Do not call")).toBeInTheDocument();
    expect(screen.getByText(/Asked not to be called/)).toBeInTheDocument();
  });
});

describe("companies list", () => {
  it("sends filters to the server and pages through results", async () => {
    const calls = mockApi({
      "GET /api/v1/auth/me": me,
      "GET /api/v1/companies": () =>
        json(page([{ ...companyDetail, channels: undefined }], 120)),
    });
    renderAt("/companies");
    expect(await screen.findByRole("link", { name: "Vet Clinic" })).toBeInTheDocument();
    expect(screen.getByText("1–50 of 120")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(screen.getByText("51–100 of 120")).toBeInTheDocument());
    await userEvent.type(screen.getByLabelText("Search"), "vet");
    await waitFor(() => expect(screen.getByText("1–50 of 120")).toBeInTheDocument()); // back to page one
    await waitFor(() => {
      const urls = calls.filter((call) => call.path === "/api/v1/companies");
      expect(urls.length).toBeGreaterThanOrEqual(3);
    });
    const spy = vi.mocked(globalThis.fetch);
    const requested = spy.mock.calls.map(([input]) => (input as Request).url);
    expect(requested.some((url) => url.includes("offset=50"))).toBe(true);
    expect(requested.some((url) => url.includes("q=vet") && url.includes("offset=0"))).toBe(true);
  });
});

describe("opportunities board", () => {
  it("asks for a reason before marking an opportunity lost", async () => {
    const stages = [
      { id: "s-new", pipeline_id: "p1", name: "New", position: 10, kind: "open" },
      { id: "s-lost", pipeline_id: "p1", name: "Lost", position: 80, kind: "lost" },
    ];
    const deal = {
      id: "d1",
      company_id: "c1",
      company_name: "Vet Clinic",
      pipeline_id: "p1",
      stage_id: "s-new",
      stage_name: "New",
      stage_kind: "open",
      title: "Booking system",
      amount: "1500.00",
      currency: "EUR",
      next_action: null,
    };
    const calls = mockApi({
      "GET /api/v1/auth/me": me,
      "GET /api/v1/pipelines": () => json([{ id: "p1", name: "Sales", is_default: true, stages }]),
      "GET /api/v1/deals": () => json(page([deal])),
      "PATCH /api/v1/deals/d1": () => json({ ...deal, stage_id: "s-lost" }),
    });
    renderAt("/opportunities");
    const select = await screen.findByRole("combobox", { name: "Stage for Booking system" });
    await userEvent.selectOptions(select, "s-lost");
    expect(calls.some((call) => call.method === "PATCH")).toBe(false); // nothing sent yet
    await userEvent.type(screen.getByLabelText(/Why was/), "Chose a competitor");
    await userEvent.click(screen.getByRole("button", { name: "Mark as lost" }));
    await waitFor(() => expect(calls.some((call) => call.method === "PATCH")).toBe(true));
    expect(calls.find((call) => call.method === "PATCH")?.body).toEqual({
      stage_id: "s-lost",
      loss_reason: "Chose a competitor",
    });
  });
});

describe("import page", () => {
  it("shows the review and imports only after confirmation", async () => {
    const ready = {
      id: "imp-1",
      kind: "xlsx",
      filename: "prospects.xlsx",
      size_bytes: 1000,
      status: "ready",
      mapping: {},
      result: {},
      error: null,
      created_at: "2026-10-08T10:00:00Z",
      committed_at: null,
      report: {
        counts: { rows: 94, create: 94, with_errors: 0, with_warnings: 0 },
        tiers: { A: 21, B: 62, C: 11 },
        confidence: { high: 31, medium: 58, low: 5 },
        missing_not_found: { email: 58 },
        mapping: { lead_id: "Lead ID" },
        unmapped_headers: [],
        score_mismatches: 0,
        shortlist: { sheet: "Shortlist", rows: 25, unresolved_lead_ids: [] },
      },
    };
    let committed = false;
    const calls = mockApi({
      "GET /api/v1/auth/me": () =>
        json(makeMe({ active: { ...TENANT, permissions: [...PERMISSIONS, "import.run"] } })),
      "POST /api/v1/imports": () => json(ready, 202),
      "GET /api/v1/imports/imp-1": () =>
        json(committed ? { ...ready, status: "committed", result: { created: 94 } } : ready),
      "POST /api/v1/imports/imp-1/commit": () => {
        committed = true;
        return json({ ...ready, status: "committed", result: { created: 94 } }, 202);
      },
    });
    renderAt("/import");
    const input = await screen.findByLabelText(/Prospect list/);
    await userEvent.upload(input, new File(["x"], "prospects.xlsx"));
    await userEvent.click(screen.getByRole("button", { name: "Check file" }));
    expect(await screen.findByText("A: 21, B: 62, C: 11")).toBeInTheDocument();
    expect(screen.getByText(/It adds\s+no leads of its own/)).toBeInTheDocument();
    expect(calls.some((call) => call.path.endsWith("/commit"))).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Import 94 leads" }));
    expect(await screen.findByText(/Import complete/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("94 added");
  });
});

describe("today call queue", () => {
  it("labels fillers, shows the due follow-up first and explains a short list", async () => {
    const entries = [
      {
        rank: 1,
        is_filler: true,
        reason: "follow_up_due",
        score_at_snapshot: 61,
        tier_at_snapshot: "B",
        prospect: makeProspect({
          lead_id: "lead-2",
          company_name: "Vet Clinic",
          total: 61,
          tier: "B",
          next_follow_up_at: "2026-10-08T07:00:00Z",
        }),
      },
      { rank: 2, is_filler: false, reason: "phone_first", score_at_snapshot: 89, tier_at_snapshot: "A", prospect: makeProspect() },
    ];
    mockApi({
      "GET /api/v1/auth/me": () =>
        json(makeMe({ active: { ...TENANT, permissions: [...PERMISSIONS, "calls.log"] } })),
      "GET /api/v1/call-queue": () =>
        json(
          makeQueue(entries, {
            shortfall: 23,
            shortfall_reason: "Only 2 prospects with a number that may be called are available. The list is not padded.",
          }),
        ),
      "GET /api/v1/prospects": () => json(page([], 21)),
    });
    renderAt("/");
    const first = await screen.findByRole("link", { name: "1. Vet Clinic" });
    expect(first).toHaveAttribute("href", "/prospects/lead-2");
    expect(screen.getByText("Tier B · 61 · filler")).toBeInTheDocument();
    expect(screen.getByText(/Follow-up due/)).toBeInTheDocument();
    expect(screen.getByText("Tier A · 89")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("The list is not padded.");
    expect(screen.getAllByText("Not yet verified")).toHaveLength(2);
    // "Open" on the first card carries the next prospect, for one-tap progress through the list.
    expect(screen.getByRole("link", { name: /Open\s*Vet Clinic/ })).toHaveAttribute(
      "href",
      "/prospects/lead-2?next=lead-1",
    );
    expect(await screen.findByText("21")).toBeInTheDocument();
  });
});

describe("prospect page", () => {
  function detail(overrides: Record<string, unknown> = {}) {
    return {
      ...makeProspect(),
      listing_url: null,
      listing_id_type: "unknown",
      scores: [],
      calls: [],
      channels: [
        channel({ id: "ch-1", raw_value: "0888 123 456", dial_uri: "tel:+359888123456" }),
        channel({ id: "ch-2", raw_value: "0884 000 004", purpose: "emergency", allow_sales_use: false, dial_uri: null }),
      ],
      ...overrides,
    };
  }
  const attempt = {
    id: "call-1",
    lead_id: "lead-1",
    company_id: "c1",
    channel_id: "ch-1",
    dialed_value: "0888 123 456",
    launched_at: "2026-10-08T09:00:00Z",
    outcome: null,
    outcome_reported_at: null,
    outcome_source: "user_reported",
    notes: null,
    follow_up_task_id: null,
    follow_up_scope: null,
    requested_email: null,
    created_by: "user-1",
    created_at: "2026-10-08T09:00:00Z",
    dial_uri: "tel:+359888123456",
  };

  it("records the dialler launch, then asks the user what happened", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined); // jsdom cannot open tel: links
    const calls = mockApi({
      "GET /api/v1/auth/me": () =>
        json(makeMe({ active: { ...TENANT, permissions: [...PERMISSIONS, "calls.log", "research.review"] } })),
      "GET /api/v1/prospects/lead-1": () => json(detail()),
      "POST /api/v1/prospects/lead-1/calls": () => json(attempt, 201),
      "POST /api/v1/calls/call-1/outcome": () => json({ ...attempt, outcome: "follow_up_requested" }),
    });
    renderAt("/prospects/lead-1");
    expect(await screen.findByText("Emergency line: not for sales calls")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /^Call / })).toHaveLength(1);
    expect(screen.getByText(/Not verified · source: imported list/)).toBeInTheDocument();
    expect(screen.getByText(/Email: No email found/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Call 0888 123 456" }));
    expect(await screen.findByText("What happened on the call?")).toBeInTheDocument();
    expect(calls.find((call) => call.path.endsWith("/calls"))?.body).toEqual({ channel_id: "ch-1" });
    expect(calls.some((call) => call.path.endsWith("/outcome"))).toBe(false); // no outcome is assumed

    await userEvent.click(screen.getByRole("button", { name: "Follow-up requested" }));
    await userEvent.type(screen.getByLabelText("Call back on"), "2026-10-10T10:30");
    await userEvent.type(screen.getByLabelText("What they asked for"), "Booking demo details");
    await userEvent.click(screen.getByRole("button", { name: "Save outcome" }));
    await waitFor(() => expect(calls.some((call) => call.path.endsWith("/outcome"))).toBe(true));
    const body = calls.find((call) => call.path.endsWith("/outcome"))?.body as Record<string, unknown>;
    expect(body.outcome).toBe("follow_up_requested");
    expect(body.follow_up_note).toBe("Booking demo details");
    expect(String(body.follow_up_at)).toMatch(/^2026-10-10T/);
    expect(body.do_not_call).toBe(false);
  });

  it("offers no call button to a read-only member or for a dismissed prospect", async () => {
    mockApi({
      "GET /api/v1/auth/me": me,
      "GET /api/v1/prospects/lead-1": () =>
        json(
          detail({
            status: "disqualified",
            actions: {
              call: { available: false, reason: "prospect_inactive" },
              email: { available: false, reason: "prospect_inactive" },
            },
            channels: [channel({ id: "ch-1", raw_value: "0888 123 456", dial_uri: null })],
          }),
        ),
    });
    renderAt("/prospects/lead-1");
    expect(await screen.findByText("Dismissed")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Call / })).not.toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Call outcome" })).not.toBeInTheDocument();
    expect(screen.getByText("Not being worked")).toBeInTheDocument();
  });
});
