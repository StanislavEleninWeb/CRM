import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppRoutes, createQueryClient } from "./App";
import { apiError, deferred, json, makeMe, mockApi, systemRoutes } from "./test/mockApi";

function renderAt(path: string) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const TENANT_A = { id: "tenant-a", name: "Acme North" };
const TENANT_B = { id: "tenant-b", name: "Acme South" };

function member(email: string, role = "owner") {
  return { user_id: `id-${email}`, email, display_name: "", role, joined_at: "2026-10-08T10:00:00Z" };
}

function page<T>(items: T[]) {
  return { items, total: items.length, limit: 50, offset: 0 };
}

afterEach(() => {
  vi.restoreAllMocks();
  document.cookie = "crm_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
});

describe("signed out", () => {
  it("offers sign-in and remembers where the user was going", async () => {
    mockApi({ "GET /api/v1/auth/me": () => apiError(401, "unauthenticated", "Sign in to continue.") });
    renderAt("/team");
    const link = await screen.findByRole("link", { name: "Sign in" });
    expect(link.getAttribute("href")).toContain("/api/v1/auth/login?return_to=");
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });

  it("shows a retryable error when the server is down", async () => {
    mockApi({ "GET /api/v1/auth/me": () => apiError(503, "service_unavailable", "Down") });
    renderAt("/");
    expect(await screen.findByRole("alert", {}, { timeout: 5000 })).toHaveTextContent("Down");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});

describe("signed in", () => {
  it("asks a user without a workspace to create one, sending the CSRF token", async () => {
    document.cookie = "crm_csrf=csrf-from-cookie; path=/";
    let created = false;
    const calls = mockApi({
      "GET /api/v1/auth/me": () => json(makeMe({ active: created ? TENANT_A : null })),
      "POST /api/v1/tenants": () => {
        created = true;
        return json({ ...TENANT_A, currency: "EUR", timezone: "Europe/Sofia" }, 201);
      },
      ...systemRoutes,
    });
    renderAt("/");
    await userEvent.type(await screen.findByLabelText("Workspace name"), "Acme North");
    await userEvent.click(screen.getByRole("button", { name: "Create workspace" }));
    expect(await screen.findByRole("heading", { name: "Today" })).toBeInTheDocument();
    const post = calls.find((call) => call.method === "POST");
    expect(post?.body).toEqual({ name: "Acme North" });
    expect(post?.headers.get("X-CSRF-Token")).toBe("csrf-from-cookie");
  });

  it("shows system details from the API on Today", async () => {
    mockApi({ "GET /api/v1/auth/me": () => json(makeMe({ active: TENANT_A })), ...systemRoutes });
    renderAt("/");
    expect(await screen.findByText("Europe/Sofia")).toBeInTheDocument();
    expect(await screen.findByText("database: ok, redis: ok")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Workspace" })).toHaveValue("tenant-a");
  });

  it("renders a not-found page for unknown routes", async () => {
    mockApi({ "GET /api/v1/auth/me": () => json(makeMe({ active: TENANT_A })) });
    renderAt("/nope");
    expect(await screen.findByText("Page not found")).toBeInTheDocument();
  });
});

describe("team page", () => {
  it("hides management controls from members who cannot manage the team", async () => {
    mockApi({
      "GET /api/v1/auth/me": () =>
        json(
          makeMe({
            active: { ...TENANT_A, role: "read_only", permissions: ["crm.read", "members.read"] },
          }),
        ),
      "GET /api/v1/members": () => json(page([member("owner@example.test"), member("v@example.test", "read_only")])),
    });
    renderAt("/team");
    expect(await screen.findByText("v@example.test")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Invitations" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: /Role for/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Remove/ })).not.toBeInTheDocument();
  });

  it("shows a one-time invitation link to a manager", async () => {
    mockApi({
      "GET /api/v1/auth/me": () => json(makeMe({ active: TENANT_A })),
      "GET /api/v1/members": () => json(page([member("owner@example.test")])),
      "GET /api/v1/invitations": () => json(page([])),
      "POST /api/v1/invitations": (call) =>
        json(
          {
            id: "inv-1",
            email: (call.body as { email: string }).email,
            role: "representative",
            status: "pending",
            expires_at: "2026-10-11T10:00:00Z",
            created_at: "2026-10-08T10:00:00Z",
            token: "tok",
            accept_url: "http://localhost:5173/invitations/accept?token=tok",
          },
          201,
        ),
    });
    renderAt("/team");
    await userEvent.type(await screen.findByLabelText("Email"), "new@example.test");
    await userEvent.click(screen.getByRole("button", { name: "Create invitation" }));
    expect(await screen.findByLabelText("Invitation link")).toHaveValue(
      "http://localhost:5173/invitations/accept?token=tok",
    );
    expect(screen.getByRole("status")).toHaveTextContent("shown only once");
  });

  it("shows a permission message when the server refuses", async () => {
    mockApi({
      "GET /api/v1/auth/me": () => json(makeMe({ active: TENANT_A })),
      "GET /api/v1/members": () => apiError(403, "permission_denied", "No access"),
      "GET /api/v1/invitations": () => json(page([])),
    });
    renderAt("/team");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("You do not have permission to view this.");
    expect(within(alert).queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });
});

describe("switching workspace", () => {
  it("never shows a late response from the previous workspace", async () => {
    let active = TENANT_A;
    const slowA = deferred<Response>();
    let memberCalls = 0;
    mockApi({
      "GET /api/v1/auth/me": () => json(makeMe({ active, tenants: [TENANT_A, TENANT_B] })),
      "GET /api/v1/members": () => {
        memberCalls += 1;
        if (active.id === TENANT_A.id) return slowA.promise; // still in flight when we switch
        return json(page([member("bob@south.example.test")]));
      },
      "GET /api/v1/invitations": () => json(page([])),
      "POST /api/v1/auth/switch-tenant": (call) => {
        active = (call.body as { tenant_id: string }).tenant_id === TENANT_B.id ? TENANT_B : TENANT_A;
        return json(null, 204);
      },
      ...systemRoutes,
    });
    renderAt("/team");
    const selector = await screen.findByRole("combobox", { name: "Workspace" });
    await waitFor(() => expect(memberCalls).toBe(1));

    await userEvent.selectOptions(selector, TENANT_B.id);
    await waitFor(() => expect(selector).toHaveValue(TENANT_B.id));
    await userEvent.click(screen.getByRole("link", { name: "Team" }));
    expect(await screen.findByText("bob@south.example.test", { selector: ".row-title" })).toBeInTheDocument();

    // Tenant A's response finally arrives. It must not be rendered.
    slowA.resolve(json(page([member("alice@north.example.test")])));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByText(/alice@north/)).not.toBeInTheDocument();
    expect(screen.getByText("bob@south.example.test", { selector: ".row-title" })).toBeInTheDocument();
  });
});

describe("invitation link", () => {
  it("tells a signed-out visitor which account to sign in with", async () => {
    mockApi({
      "GET /api/v1/auth/me": () => apiError(401, "unauthenticated", "Sign in to continue."),
      "GET /api/v1/invitations/lookup": () =>
        json({ tenant_name: "Acme North", email: "rep@example.test", role: "representative", status: "pending" }),
    });
    renderAt(`/invitations/accept?token=${"t".repeat(43)}`);
    expect(await screen.findByText("rep@example.test")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in to accept" })).toBeInTheDocument();
  });

  it("explains an email mismatch instead of offering to accept", async () => {
    mockApi({
      "GET /api/v1/auth/me": () => json(makeMe({ email: "someone@example.test" })),
      "GET /api/v1/invitations/lookup": () =>
        json({ tenant_name: "Acme North", email: "rep@example.test", role: "representative", status: "pending" }),
    });
    renderAt(`/invitations/accept?token=${"t".repeat(43)}`);
    expect(await screen.findByText(/different email address/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept invitation" })).not.toBeInTheDocument();
  });
});
