import { useMutation } from "@tanstack/react-query";

import { api, type Schemas, unwrap } from "../api/client";
import { useAuth, useTenantQuery } from "../auth/AuthContext";
import { ErrorState, Loading } from "../components/States";
import { formatDate } from "../lib/format";

type Billing = Schemas["BillingOut"];

const COUNTERS: [string, string][] = [
  ["seats", "Members and open invitations"],
  ["research_runs_this_month", "Research runs this month"],
  ["emails_this_month", "Emails this month"],
  ["api_keys", "API keys"],
  ["webhook_endpoints", "Webhook addresses"],
];

/** A one-line statement of where the workspace stands, shown on every page when it matters. */
export function StandingBanner() {
  const { can } = useAuth();
  const standing = useTenantQuery(["entitlements"], () => unwrap(api.GET("/api/v1/entitlements")));
  const data = standing.data;
  if (!data || !data.billing_enforced || (data.standing === "good" && data.status !== "trialing")) return null;
  const text =
    data.standing === "restricted"
      ? `${data.reason} Everything stays readable and can be exported; nothing new can be added or sent.`
      : data.standing === "grace"
        ? `${data.reason} Work continues until ${formatDate(data.grace_until)}.`
        : `Trial until ${formatDate(data.trial_ends_at)}.`;
  return (
    <p className={data.standing === "good" ? "notice" : "notice notice-warn"} role="status">
      {text} {can("tenant.billing") ? <a href="/billing">Billing</a> : "Ask the workspace owner."}
    </p>
  );
}

export function BillingPage() {
  const billing = useTenantQuery(["billing"], () => unwrap(api.GET("/api/v1/billing")));
  const go = useMutation({
    mutationFn: (target: { plan?: string }) =>
      target.plan
        ? unwrap(api.POST("/api/v1/billing/checkout", { body: { plan_code: target.plan } }))
        : unwrap(api.POST("/api/v1/billing/portal")),
    // The payment pages belong to the billing provider. Coming back from them changes nothing here by itself.
    onSuccess: (redirect) => window.location.assign(redirect.url),
  });
  const sync = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/billing/sync")),
    onSuccess: () => billing.refetch(),
  });

  if (billing.isPending) return <Loading label="Loading billing" />;
  if (billing.isError) return <ErrorState error={billing.error} onRetry={() => void billing.refetch()} />;
  const data: Billing = billing.data;

  return (
    <>
      <h1>Billing</h1>
      {!data.billing_enforced ? (
        <p className="notice">Billing is switched off on this installation. Nothing is charged and no limits apply.</p>
      ) : (
        <>
          <section className="panel" aria-labelledby="standing-heading">
            <h2 id="standing-heading">
              {data.plan_name}
              {data.is_test_plan ? " — test plan, no approved price" : ""}
            </h2>
            <p className={data.standing === "good" ? "notice" : "notice notice-warn"}>
              {data.standing === "good" ? `In good standing (${data.status.replace(/_/g, " ")}).` : data.reason}
              {data.status === "trialing" && data.trial_ends_at ? ` Trial ends ${formatDate(data.trial_ends_at)}.` : ""}
              {data.grace_until ? ` Work continues until ${formatDate(data.grace_until)}.` : ""}
              {data.current_period_end ? ` Paid until ${formatDate(data.current_period_end)}.` : ""}
            </p>
            {data.notes.map((note) => (
              <p key={note} className="muted">
                {note}
              </p>
            ))}
            {data.sync_error ? <p className="notice notice-warn">{data.sync_error}</p> : null}
            <dl className="facts">
              {COUNTERS.map(([key, label]) => (
                <div key={key}>
                  <dt>{label}</dt>
                  <dd>
                    {data.usage[key] ?? 0} of {data.limits[key] ?? "no limit"}
                  </dd>
                </div>
              ))}
            </dl>
            <p className="muted">Going over a limit never adds a charge: the action is refused. Nothing is deleted when a payment fails.</p>
            <div className="row-actions">
              {data.has_billing_customer ? (
                <button type="button" className="button" disabled={go.isPending} onClick={() => go.mutate({})}>
                  Payment method, invoices and cancellation
                </button>
              ) : null}
              <button type="button" className="button" disabled={sync.isPending} onClick={() => sync.mutate()}>
                Check with the billing provider now
              </button>
            </div>
          </section>
          <section className="panel" aria-labelledby="plans-heading">
            <h2 id="plans-heading">Plans</h2>
            <ul className="rows">
              {data.plans.map((plan) => (
                <li key={plan.code} className="row">
                  <div className="row-main">
                    <span className="row-title">{plan.name}</span>
                    <span className="badge badge-warn">{plan.price_note}</span>
                    <span className="muted">
                      {plan.seats_included} seats · {plan.research_runs_per_month} research runs and {plan.emails_per_month} emails a month
                    </span>
                  </div>
                  <div className="row-actions">
                    <button
                      type="button"
                      className="button"
                      disabled={go.isPending || !plan.purchasable || plan.code === data.plan_code}
                      onClick={() => go.mutate({ plan: plan.code })}
                    >
                      {plan.code === data.plan_code ? "Current plan" : plan.purchasable ? "Choose" : "Not available yet"}
                      <span className="visually-hidden"> {plan.name}</span>
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </section>
        </>
      )}
      {go.isError ? <ErrorState error={go.error} /> : null}
      {sync.isError ? <ErrorState error={sync.error} /> : null}
    </>
  );
}
