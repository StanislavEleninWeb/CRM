import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppRoutes, createQueryClient } from "../App";
import { json, makeMe, mockApi } from "../test/mockApi";

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
