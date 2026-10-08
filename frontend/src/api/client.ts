import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

export type ErrorEnvelope = components["schemas"]["ErrorEnvelope"];

/** An API failure carrying the server's error envelope. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly correlationId: string | undefined;
  readonly details: ErrorEnvelope["error"]["details"];

  constructor(status: number, envelope: ErrorEnvelope | undefined) {
    super(envelope?.error.message ?? `Request failed (${status})`);
    this.name = "ApiError";
    this.status = status;
    this.code = envelope?.error.code ?? "unknown_error";
    this.correlationId = envelope?.error.correlation_id ?? undefined;
    this.details = envelope?.error.details;
  }
}

export const api = createClient<paths>({
  baseUrl: window.location.origin,
  credentials: "same-origin",
  // Resolved per call so the global can be replaced (tests, instrumentation).
  fetch: (request) => globalThis.fetch(request),
});

/** Unwrap an openapi-fetch result, throwing ApiError on failure. */
export async function unwrap<T>(
  call: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  let result: { data?: T; error?: unknown; response: Response };
  try {
    result = await call;
  } catch {
    throw new ApiError(0, {
      error: { code: "network_error", message: "The server could not be reached." },
    });
  }
  if (result.error !== undefined || !result.response.ok) {
    throw new ApiError(result.response.status, result.error as ErrorEnvelope | undefined);
  }
  return result.data as T;
}
