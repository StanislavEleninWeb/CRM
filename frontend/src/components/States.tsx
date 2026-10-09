import type { ReactNode } from "react";

import { ApiError } from "../api/client";

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="state" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      {label}…
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="state state-empty">
      <p className="state-title">{title}</p>
      {children}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const apiError = error instanceof ApiError ? error : undefined;
  const forbidden = apiError?.status === 403;
  return (
    <div className="state state-error" role="alert">
      <p className="state-title">
        {forbidden
          ? "You do not have permission to view this."
          : apiError?.status === 402
            ? "The subscription does not allow this right now."
            : apiError?.code === "plan_limit_reached"
              ? "The plan's limit has been reached."
              : "Something went wrong."}
      </p>
      <p>{error instanceof Error ? error.message : "Unknown error"}</p>
      {apiError?.correlationId ? (
        <p className="muted">
          Reference: <code>{apiError.correlationId}</code>
        </p>
      ) : null}
      {onRetry && !forbidden ? (
        <button type="button" className="button" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}
