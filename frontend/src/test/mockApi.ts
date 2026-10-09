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
      body: parseBody(text),
    };
    calls.push(call);
    const handler = routes[`${call.method} ${call.path}`] ?? DEFAULT_ROUTES[`${call.method} ${call.path}`];
    if (!handler) return apiError(500, "unmocked", `No mock for ${call.method} ${call.path}`);
    return handler(call);
  });
  return calls;
}

function parseBody(text: string): unknown {
  if (!text) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return text; // multipart uploads are not JSON
  }
}

export const goodStanding = {
  billing_enforced: false,
  standing: "good",
  reason: null,
  plan_code: "internal_pilot",
  plan_name: "Internal pilot",
  is_test_plan: false,
  status: "not_billed",
  trial_ends_at: null,
  grace_until: null,
  current_period_end: null,
  cancel_at_period_end: false,
  limits: { seats: null, research_runs_this_month: null, emails_this_month: null, api_keys: null, webhook_endpoints: null },
  usage: { seats: 1, research_runs_this_month: 0, emails_this_month: 0, api_keys: 0, webhook_endpoints: 0 },
  over_limit: [],
  notes: [],
};

/** Shown on every page, so every test would otherwise have to mock it. */
const DEFAULT_ROUTES: Record<string, Handler> = { "GET /api/v1/entitlements": () => json(goodStanding) };

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

export function makeProspect(overrides: Record<string, unknown> = {}) {
  return {
    lead_id: "lead-1",
    external_id: "P-001",
    status: "qualified",
    outreach_status: "not_contacted",
    owner_user_id: null,
    next_action: null,
    next_action_at: null,
    company_id: "c1",
    company_name: "Salon Aurora",
    city: "Sofia",
    country: "Bulgaria",
    business_type: "Hair salon",
    industry_group: "Beauty & wellness",
    website_url: null,
    checked_on: "2026-10-07",
    is_stale: false,
    confidence: "high",
    source_type: "user_import",
    verification_state: "unverified",
    website_status_raw: "Loads (HTTPS)",
    website_base: "loads",
    total: 89,
    tier: "A",
    components: { evidence: 28, relevance: 23, value: 15, reachability: 14, activity: 9 },
    score_origin: "import",
    finding: "Homepage shows template filler text.",
    finding_state: "unverified",
    evidence_url: "https://example-salon.bg/",
    observation_id: "obs-1",
    hypothesis: "Booking is done by phone only.",
    hypothesis_status: "unconfirmed",
    hypothesis_id: "hyp-1",
    service_category: "Online booking",
    recommended_service_raw: "Online booking",
    fit_explanation: "Active salon.",
    proposed_benefit: "Clients book at any hour.",
    preferred_channel: "phone",
    fallback_channels: [],
    channel_instruction: null,
    outreach_opening: "Здравейте!",
    discovery_question: "How are bookings handled?",
    notes: null,
    tags: ["Other"],
    phone_count: 1,
    dialable_count: 1,
    email_count: 0,
    restriction_reasons: null,
    next_follow_up_at: null,
    open_follow_ups: 0,
    last_outcome_at: null,
    needs_verification: ["unverified_high_priority"],
    actions: {
      call: { available: true, reason: null },
      email: { available: false, reason: "no_email_found" },
    },
    user_edited_fields: [],
    ...overrides,
  };
}

export function makeQueue(entries: Record<string, unknown>[] = [], extra: Record<string, unknown> = {}) {
  return {
    queue_date: "2026-10-08",
    timezone: "Europe/Sofia",
    requested_size: 25,
    tie_break: "score (high to low), confidence, most recently checked, Lead ID",
    rules: ["A follow-up that is due comes first, whatever happened before."],
    shortfall: 0,
    shortfall_reason: null,
    entries,
    ...extra,
  };
}

/** The requests the Today page makes, answered with an empty workspace. */
export const systemRoutes: Record<string, Handler> = {
  "GET /api/v1/call-queue": () => json(makeQueue()),
  "GET /api/v1/prospects": () => json({ items: [], total: 0, limit: 1, offset: 0 }),
};
