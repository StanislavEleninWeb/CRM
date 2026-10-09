import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppRoutes, createQueryClient } from "../App";
import { json, makeMe, makeQueue, mockApi } from "../test/mockApi";

const me = (permissions: string[]) => () =>
  json(makeMe({ active: { id: "tenant-a", name: "SEWEB", permissions: ["crm.read", ...permissions] } }));

function renderAt(path: string) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const metric = (overrides: Record<string, unknown>) => ({
  key: "reported_calls", label: "Calls reported", value: 5, unit: "count", numerator: null, denominator: null,
  definition: "Call attempts for which a person reported an outcome in the period.", basis: "reported_by_people", note: null, drill: "reported_calls",
  ...overrides,
});
const report = {
  date_from: "2026-10-01", date_to: "2026-10-31", timezone: "Europe/Sofia",
  cohort_rule: "Each figure counts events whose own date falls on 2026-10-01 to 2026-10-31 inclusive, as whole days in Europe/Sofia.",
  metrics: [
    metric({}),
    metric({ key: "connected_call_rate", label: "Connected-call rate", value: 60, unit: "percent", numerator: 3, denominator: 5, drill: "connected_calls",
             definition: "Reported calls in which someone was reached, divided by all calls with a reported outcome." }),
    metric({ key: "follow_up_completion", label: "Follow-ups completed", value: null, unit: "percent", numerator: 0, denominator: 0, drill: null,
             definition: "Of the follow-up tasks due in the period, those marked done." }),
  ],
  stages: [{ stage: "Proposal", kind: "open", entered_in_period: 2, open_now: 1, average_days_in_stage: 4.5, oldest_days_in_stage: 4.5 }],
  won_amounts: [{ currency: "EUR", amount: "1500.00", basis: "entered on the opportunity" }, { currency: "USD", amount: "300.00", basis: "entered on the opportunity" }],
  research_costs: [],
  not_collected: ["Email opens and clicks: not collected. They would be approximate at best."],
};

afterEach(() => vi.restoreAllMocks());

describe("reports", () => {
  it("shows each figure with what it counts, says no data instead of zero, and opens the records behind it", async () => {
    const calls = mockApi({
      "GET /api/v1/auth/me": me(["reports.read"]),
      "GET /api/v1/reports/funnel": () => json(report),
      "GET /api/v1/reports/records": () =>
        json([{ kind: "call", id: "c-1", label: "Salon Aurora - connected", at: "2026-10-06T08:05:00Z", link: "/prospects/lead-1" }]),
    });
    renderAt("/reports");
    expect(await screen.findByText("Connected-call rate: 60% (3 of 5)")).toBeInTheDocument();
    expect(screen.getByText("Follow-ups completed: No data")).toBeInTheDocument(); // not "0%"
    expect(screen.queryByText(/Follow-ups completed: 0/)).not.toBeInTheDocument();
    expect(screen.getByText(/divided by all calls with a reported outcome/)).toBeInTheDocument();
    expect(screen.getAllByText(/Reported by people/).length).toBeGreaterThan(0);
    expect(screen.getByText("1500.00 EUR")).toBeInTheDocument();
    expect(screen.getByText("300.00 USD")).toBeInTheDocument();
    expect(screen.getByText("Currencies are not added together.")).toBeInTheDocument();
    expect(screen.getByText(/whole days in Europe\/Sofia/)).toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: /Show records/ })[1]!);
    expect(await screen.findByRole("link", { name: "Salon Aurora - connected" })).toHaveAttribute("href", "/prospects/lead-1");
    expect(calls.some((call) => call.path.endsWith("/reports/records"))).toBe(true);
  });

  it("puts what needs attention at the top of today", async () => {
    mockApi({
      "GET /api/v1/auth/me": me([]),
      "GET /api/v1/call-queue": () => json(makeQueue()),
      "GET /api/v1/prospects": () => json({ items: [], total: 0, limit: 1, offset: 0 }),
      "GET /api/v1/workspace/attention": () =>
        json({ generated_at: "2026-10-09T08:00:00Z", items: [{ key: "follow_ups", label: "Follow-ups due", count: 2, link: "/tasks", examples: ["Call back about booking"] }] }),
    });
    renderAt("/");
    expect(await screen.findByRole("link", { name: "2 · Follow-ups due" })).toHaveAttribute("href", "/tasks");
    expect(screen.getByText("Call back about booking")).toBeInTheDocument();
  });
});

describe("data and access", () => {
  it("lets the owner allow one person in for a limited time and says what deletion does not reach", async () => {
    let grants: unknown[] = [];
    const calls = mockApi({
      "GET /api/v1/auth/me": me(["tenant.settings", "owners.manage"]),
      "GET /api/v1/retention": () =>
        json({ email_content_days: 730, webhook_delivery_days: 30, outbox_event_days: 90, finished_job_days: 14, idempotency_hours: 24, audit_days: 730,
               bounds: { email_content_days: [30, 3650], audit_days: [365, 3650], webhook_delivery_days: [1, 365], outbox_event_days: [7, 365] },
               not_covered: ["Backups: a deleted record remains in encrypted backups until they expire."] }),
      "GET /api/v1/support-grants": () => json(grants),
      "POST /api/v1/support-grants": (call) => {
        grants = [{ id: "g-1", ...(call.body as object), created_at: "2026-10-09T08:00:00Z", expires_at: "2026-10-09T10:00:00Z", revoked_at: null,
                    first_used_at: null, active: true }];
        return json(grants[0], 201);
      },
      "GET /api/v1/tenant/deletion": () =>
        json({ requested_at: null, due_at: null, grace_days: 7, what_happens: ["For seven days nothing changes and the request can be cancelled."] }),
    });
    renderAt("/data");
    expect(await screen.findByText(/Backups: a deleted record remains in encrypted backups/)).toBeInTheDocument();
    const form = await screen.findByRole("form", { name: "Allow support access" });
    await userEvent.type(within(form).getByLabelText("Person's email"), "helper@example.test");
    await userEvent.clear(within(form).getByLabelText("Hours (1 to 72)"));
    await userEvent.type(within(form).getByLabelText("Hours (1 to 72)"), "2");
    await userEvent.type(within(form).getByLabelText("Why"), "Import shows no rows");
    await userEvent.click(within(form).getByRole("button", { name: "Allow access" }));
    await waitFor(() => expect(calls.some((call) => call.method === "POST")).toBe(true));
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({
      grantee_email: "helper@example.test", hours: 2, reason: "Import shows no rows", include_communications: false,
    });
    expect(await screen.findByText(/Import shows no rows · until/)).toBeInTheDocument();
    // Deleting the workspace needs its name typed exactly.
    const schedule = screen.getByRole("button", { name: "Schedule deletion" });
    expect(schedule).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Type the workspace name to confirm"), "SEWEB");
    expect(schedule).toBeEnabled();
  });
});
