import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppRoutes, createQueryClient } from "../App";
import { json, makeMe, mockApi } from "../test/mockApi";

const me = () =>
  json(makeMe({ active: { id: "tenant-a", name: "SEWEB", permissions: ["crm.read", "integrations.manage", "reports.read"] } }));
const base = {
  "GET /api/v1/auth/me": me,
  "GET /api/v1/mailboxes": () => json([]),
  "GET /api/v1/mailboxes/gmail/availability": () => json({ available: false, reason: "Gmail is not configured.", internal_domain: null }),
  "GET /api/v1/provider-adapters": () => json([]),
  "GET /api/v1/provider-connections": () => json([]),
  "GET /api/v1/budgets": () => json([]),
  "GET /api/v1/usage/summary": () =>
    json({ currency: "EUR", period_start: "2026-10-01", verified_amount: "0", estimated_amount: "0", unknown_reserved_amount: "0", reserved_amount: "0", by_provider: [], entries: 0 }),
};
const SECRET_KEY = "crm_EXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE0";

function renderPage() {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={["/integrations"]}>
        <AppRoutes />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("API keys and webhooks", () => {
  it("shows a new key once, then only its prefix, and offers only scopes the person may grant", async () => {
    const listed = {
      id: "k-1", name: "Hermes", prefix: "crm_EXAMPL", scopes: ["crm.read"], rate_limit_per_minute: 120,
      created_at: "2026-10-09T08:00:00Z", expires_at: "2027-01-07T08:00:00Z", revoked_at: null, last_used_at: null, usable: true,
    };
    let keys: unknown[] = [];
    const calls = mockApi({
      ...base,
      "GET /api/v1/api-keys": () => json(keys),
      "GET /api/v1/api-keys/scopes": () =>
        json([{ name: "crm.read", grantable: true }, { name: "crm.write", grantable: true }, { name: "research.run", grantable: false }]),
      "POST /api/v1/api-keys": () => {
        keys = [listed];
        return json({ ...listed, key: SECRET_KEY }, 201);
      },
      "GET /api/v1/webhook-endpoints": () => json([]),
      "GET /api/v1/webhook-deliveries": () => json({ items: [], total: 0, limit: 20, offset: 0 }),
    });
    renderPage();
    const form = await screen.findByRole("form", { name: "Create an API key" });
    expect(await within(form).findByLabelText("research.run")).toBeDisabled();
    await userEvent.type(within(form).getByLabelText("What the key is for"), "Hermes");
    await userEvent.click(within(form).getByLabelText("crm.write"));
    await userEvent.click(within(form).getByRole("button", { name: "Create key" }));
    await waitFor(() => expect(calls.some((call) => call.method === "POST")).toBe(true));
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({ name: "Hermes", scopes: ["crm.read", "crm.write"], expires_in_days: 90 });

    expect(await screen.findByText(SECRET_KEY)).toBeInTheDocument();
    expect(screen.getByText(/cannot be shown again/)).toBeInTheDocument();
    expect(await screen.findByText(/crm_EXAMPL… · crm\.read/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "I have copied it" }));
    expect(screen.queryByText(SECRET_KEY)).not.toBeInTheDocument();
    expect(window.localStorage.length + window.sessionStorage.length).toBe(0); // never kept in the browser
  });

  it("shows failing deliveries with their reason and replays one on request", async () => {
    const endpoint = {
      id: "w-1", url: "https://hooks.customer.example/crm", description: null, event_types: ["*"], status: "active",
      consecutive_failures: 7, last_success_at: null, last_failure_at: "2026-10-09T09:00:00Z", last_error: "The receiver answered 500.",
      old_signature_until: null, created_at: "2026-10-09T08:00:00Z",
    };
    const dead = {
      id: "d-1", endpoint_id: "w-1", event_id: "e-1", event_type: "call.outcome_reported", status: "dead", attempts: 7,
      next_attempt_at: null, last_status_code: 500, last_error: "The receiver answered 500.", delivered_at: null, created_at: "2026-10-09T08:00:00Z",
    };
    const calls = mockApi({
      ...base,
      "GET /api/v1/api-keys": () => json([]),
      "GET /api/v1/api-keys/scopes": () => json([]),
      "GET /api/v1/webhook-endpoints": () => json([endpoint]),
      "GET /api/v1/webhook-deliveries": () => json({ items: [dead], total: 1, limit: 20, offset: 0 }),
      "POST /api/v1/webhook-deliveries/d-1/replay": () => json({ ...dead, status: "pending", attempts: 0 }),
      "POST /api/v1/webhook-endpoints": () =>
        new Response(JSON.stringify({ error: { code: "validation_error", message: "This address cannot be used: the address is not public." } }), {
          status: 422,
          headers: { "Content-Type": "application/json" },
        }),
    });
    renderPage();
    expect(await screen.findByText("7 failed in a row: The receiver answered 500.")).toBeInTheDocument();
    expect(await screen.findByText("Gave up after 7 attempts: The receiver answered 500.")).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: /Send again/ }));
    await waitFor(() => expect(calls.some((call) => call.path.endsWith("/replay"))).toBe(true));

    await userEvent.type(screen.getByLabelText("HTTPS address"), "https://10.0.0.5/hook");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByText("This address cannot be used: the address is not public.")).toBeInTheDocument();
  });
});
