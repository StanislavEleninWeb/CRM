import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppRoutes, createQueryClient } from "./App";

function renderAt(path: string) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => vi.restoreAllMocks());

describe("application shell", () => {
  it("shows system details returned by the API", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.endsWith("/api/v1/system/info")) {
        return jsonResponse({
          name: "SEWEB CRM",
          api_version: "v1",
          environment: "test",
          default_currency: "EUR",
          default_timezone: "Europe/Sofia",
        });
      }
      return jsonResponse({ status: "ready", checks: { database: "ok", redis: "ok" } });
    });
    renderAt("/");
    expect(screen.getByRole("status")).toHaveTextContent("Loading system details");
    expect(await screen.findByText("Europe/Sofia")).toBeInTheDocument();
    expect(await screen.findByText("database: ok, redis: ok")).toBeInTheDocument();
  });

  it("shows the server error with its reference and offers a retry", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      jsonResponse(
        { error: { code: "permission_denied_test", message: "Boom", correlation_id: "corr-1234" } },
        400,
      ),
    );
    renderAt("/");
    expect(await screen.findByRole("alert")).toHaveTextContent("Boom");
    expect(screen.getByText("corr-1234")).toBeInTheDocument();
    const callsBefore = fetchMock.mock.calls.length;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(fetchMock.mock.calls.length).toBeGreaterThan(callsBefore);
  });

  it("renders a not-found page for unknown routes", () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => jsonResponse({}));
    renderAt("/nope");
    expect(screen.getByText("Page not found")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
  });
});
