import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppRoutes, createQueryClient } from "../App";
import { apiError, goodStanding, json, makeMe, makeQueue, mockApi } from "../test/mockApi";

const me = (permissions: string[]) => () =>
  json(makeMe({ active: { id: "tenant-a", name: "SEWEB", permissions: ["crm.read", ...permissions] } }));
const trial = { ...goodStanding, billing_enforced: true, plan_code: "trial", plan_name: "Trial", is_test_plan: true, status: "trialing",
  trial_ends_at: "2026-10-23T08:00:00Z", limits: { seats: 3, research_runs_this_month: 5, emails_this_month: 50, api_keys: 1, webhook_endpoints: 1 } };
const restricted = { ...trial, standing: "restricted", reason: "The trial has ended. Choose a plan to continue." };
const plans = [
  { code: "test_starter", name: "Starter (test)", is_test: true, price_note: "TEST PRICE - not an approved price", seats_included: 3,
    research_runs_per_month: 20, emails_per_month: 300, api_keys: 2, webhook_endpoints: 2, purchasable: true },
  { code: "test_team", name: "Team (test)", is_test: true, price_note: "TEST PRICE - not an approved price", seats_included: 10,
    research_runs_per_month: 100, emails_per_month: 2000, api_keys: 10, webhook_endpoints: 10, purchasable: false },
];

function renderAt(path: string) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("billing", () => {
  it("labels test plans, shows use against limits and sends the owner to the provider for payment", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign, origin: window.location.origin });
    const calls = mockApi({
      "GET /api/v1/auth/me": me(["tenant.billing"]),
      "GET /api/v1/entitlements": () => json(trial),
      "GET /api/v1/billing": () => json({ ...trial, plans, has_billing_customer: false, sync_error: null, paid_overage: "never" }),
      "POST /api/v1/billing/checkout": () => json({ url: "https://checkout.stripe.test/session/1" }),
    });
    renderAt("/billing");
    expect(await screen.findByRole("heading", { name: "Trial — test plan, no approved price" })).toBeInTheDocument();
    expect(screen.getAllByText("TEST PRICE - not an approved price")).toHaveLength(2);
    expect(screen.getByText("1 of 3")).toBeInTheDocument();
    expect(screen.getByText(/Going over a limit never adds a charge/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Not available yet/ })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /Payment method/ })).not.toBeInTheDocument(); // no billing account yet
    await userEvent.click(screen.getByRole("button", { name: /^Choose/ }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("https://checkout.stripe.test/session/1"));
    // Only the plan is named. The price is the server's business.
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({ plan_code: "test_starter" });
    vi.unstubAllGlobals();
  });

  it("tells every member plainly when the workspace is restricted, and says what still works", async () => {
    mockApi({
      "GET /api/v1/auth/me": me([]),
      "GET /api/v1/entitlements": () => json(restricted),
      "GET /api/v1/call-queue": () => json(makeQueue()),
    });
    renderAt("/");
    const banner = await screen.findByText(/The trial has ended\. Choose a plan to continue\./);
    expect(banner).toHaveTextContent("Everything stays readable and can be exported; nothing new can be added or sent.");
    expect(banner).toHaveTextContent("Ask the workspace owner."); // this member cannot open billing
    expect(screen.queryByRole("link", { name: "Billing" })).not.toBeInTheDocument();
  });

  it("explains a refused action instead of a generic failure, and shows nothing when billing is off", async () => {
    mockApi({
      "GET /api/v1/auth/me": me(["tenant.billing"]),
      "GET /api/v1/billing": () => apiError(402, "subscription_required", "The trial has ended. Choose a plan to continue."),
    });
    renderAt("/billing");
    expect(await screen.findByText("The subscription does not allow this right now.")).toBeInTheDocument();
    expect(screen.queryByText(/Trial until/)).not.toBeInTheDocument(); // default standing: billing off, no banner
  });
});
