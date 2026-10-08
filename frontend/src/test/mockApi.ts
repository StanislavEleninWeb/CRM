import { vi } from "vitest";

export interface Call {
  method: string;
  path: string;
  headers: Headers;
  body: unknown;
}

type Handler = (call: Call) => Response | Promise<Response>;

export function json(body: unknown, status = 200) {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function apiError(status: number, code: string, message: string) {
  return json({ error: { code, message, correlation_id: "corr-test" } }, status);
}

/** Replace fetch with route handlers keyed by "METHOD /path". Unknown routes fail loudly. */
export function mockApi(routes: Record<string, Handler>) {
  const calls: Call[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const request = input instanceof Request ? input : new Request(String(input), init);
    const url = new URL(request.url);
    const text = request.method === "GET" ? "" : await request.clone().text();
    const call: Call = {
      method: request.method,
      path: url.pathname,
      headers: request.headers,
      body: text ? JSON.parse(text) : undefined,
    };
    calls.push(call);
    const handler = routes[`${call.method} ${call.path}`];
    if (!handler) return apiError(500, "unmocked", `No mock for ${call.method} ${call.path}`);
    return handler(call);
  });
  return calls;
}

export function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

const OWNER_PERMISSIONS = ["crm.read", "members.read", "members.manage", "owners.manage"];

export function makeMe(options: {
  active?: { id: string; name: string; role?: string; permissions?: string[] } | null;
  tenants?: { id: string; name: string; role?: string }[];
  email?: string;
}) {
  const active = options.active ?? null;
  return {
    user: { id: "user-1", email: options.email ?? "owner@example.test", display_name: "Olga Owner" },
    active_tenant: active
      ? {
          id: active.id,
          name: active.name,
          role: active.role ?? "owner",
          permissions: active.permissions ?? OWNER_PERMISSIONS,
          currency: "EUR",
          timezone: "Europe/Sofia",
        }
      : null,
    tenants: (options.tenants ?? (active ? [active] : [])).map((tenant) => ({
      id: tenant.id,
      name: tenant.name,
      role: tenant.role ?? "owner",
    })),
    csrf_token: "csrf-1",
    mfa_claimed: false,
  };
}

export const systemRoutes: Record<string, Handler> = {
  "GET /api/v1/system/info": () =>
    json({
      name: "SEWEB CRM",
      api_version: "v1",
      environment: "test",
      default_currency: "EUR",
      default_timezone: "Europe/Sofia",
    }),
  "GET /api/v1/system/readiness": () =>
    json({ status: "ready", checks: { database: "ok", redis: "ok" } }),
};
