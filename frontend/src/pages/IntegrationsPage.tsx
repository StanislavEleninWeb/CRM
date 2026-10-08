import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";

import { api, type Schemas, unwrap } from "../api/client";
import { tenantKey, useAuth, useTenantQuery } from "../auth/AuthContext";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { formatDateTime, formatMoney } from "../lib/format";

type Connection = Schemas["ConnectionOut"];

export function IntegrationsPage() {
  const { can } = useAuth();
  return (
    <>
      <h1>Integrations and spend</h1>
      <Mailboxes />
      {can("integrations.manage") ? <Connections /> : null}
      <Budgets />
      <Usage />
    </>
  );
}

function Connections() {
  const { tenantId, me } = useAuth();
  const queryClient = useQueryClient();
  const key = ["provider-connections"];
  const connections = useTenantQuery(key, () => unwrap(api.GET("/api/v1/provider-connections")));
  const adapters = useTenantQuery(["provider-adapters"], () => unwrap(api.GET("/api/v1/provider-adapters")));
  const refresh = () => queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, key) });
  const [provider, setProvider] = useState("");
  const [label, setLabel] = useState("");
  const [credential, setCredential] = useState("");
  const ids = { provider: useId(), label: useId(), credential: useId() };

  const create = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/provider-connections", {
          body: { provider: provider || (adapters.data?.[0]?.name ?? ""), label, access_mode: "byok", credential },
        }),
      ),
    onSuccess: () => {
      setCredential("");
      setLabel("");
      void refresh();
    },
  });
  const act = useMutation({
    mutationFn: ({ id, action }: { id: string; action: "check" | "revoke" }) =>
      action === "check"
        ? unwrap(api.POST("/api/v1/provider-connections/{connection_id}/check", { params: { path: { connection_id: id } } }))
        : unwrap(api.DELETE("/api/v1/provider-connections/{connection_id}", { params: { path: { connection_id: id } } })),
    onSettled: refresh,
  });

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    create.mutate();
  }

  return (
    <section className="panel" aria-labelledby="connections-heading">
      <h2 id="connections-heading">Provider connections</h2>
      <p className="muted">
        Keys are encrypted and never shown again. A chat subscription with an AI vendor does not include API access;
        an API key comes from the vendor&apos;s developer console.
      </p>
      {connections.isPending ? (
        <Loading label="Loading connections" />
      ) : connections.isError ? (
        <ErrorState error={connections.error} onRetry={() => void connections.refetch()} />
      ) : connections.data.length === 0 ? (
        <EmptyState title="No providers connected." />
      ) : (
        <ul className="rows">
          {connections.data.map((connection: Connection) => (
            <li key={connection.id} className="row">
              <div className="row-main">
                <span className="row-title">{connection.label}</span>
                <span className="muted">
                  {connection.provider} · {connection.purpose} · key {connection.credential_hint ?? "not stored"}
                  {connection.is_local_test_adapter ? " · local test adapter, not a real provider" : ""}
                </span>
                <span className="muted">
                  Last checked {formatDateTime(connection.last_checked_at, me.active_tenant?.timezone)}
                  {connection.last_error ? ` · ${connection.last_error}` : ""}
                </span>
              </div>
              <span className={connection.status === "active" ? "badge tier-A" : "badge badge-warn"}>{connection.status}</span>
              <div className="row-actions">
                {connection.status !== "revoked" ? (
                  <>
                    <button type="button" className="button" disabled={act.isPending} onClick={() => act.mutate({ id: connection.id, action: "check" })}>
                      Check<span className="visually-hidden"> {connection.label}</span>
                    </button>
                    <button
                      type="button"
                      className="button"
                      disabled={act.isPending}
                      onClick={() => {
                        if (window.confirm(`Revoke ${connection.label} and erase its key?`)) act.mutate({ id: connection.id, action: "revoke" });
                      }}
                    >
                      Revoke<span className="visually-hidden"> {connection.label}</span>
                    </button>
                  </>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      )}
      {act.isError ? <ErrorState error={act.error} /> : null}
      <form className="inline-form" onSubmit={onSubmit} aria-label="Connect a provider" autoComplete="off">
        <div className="field">
          <label htmlFor={ids.provider}>Provider</label>
          <select id={ids.provider} value={provider} onChange={(e) => setProvider(e.target.value)}>
            {(adapters.data ?? []).map((adapter) => (
              <option key={adapter.name} value={adapter.name}>
                {adapter.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor={ids.label}>Name</label>
          <input id={ids.label} required maxLength={120} value={label} onChange={(e) => setLabel(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor={ids.credential}>API key</label>
          <input
            id={ids.credential}
            type="password"
            required
            minLength={8}
            autoComplete="off"
            value={credential}
            onChange={(e) => setCredential(e.target.value)}
          />
        </div>
        <button type="submit" className="button button-primary" disabled={create.isPending}>
          Connect
        </button>
      </form>
      {create.isError ? <ErrorState error={create.error} /> : null}
    </section>
  );
}

function Budgets() {
  const { tenantId, can } = useAuth();
  const queryClient = useQueryClient();
  const budgets = useTenantQuery(["budgets"], () => unwrap(api.GET("/api/v1/budgets")));
  const [limit, setLimit] = useState("");
  const limitId = useId();
  const save = useMutation({
    mutationFn: () =>
      unwrap(api.PUT("/api/v1/budgets", { body: { scope: "research", period: "month", limit_amount: limit } })),
    onSuccess: () => {
      setLimit("");
      void queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, ["budgets"]) });
    },
  });
  return (
    <section className="panel" aria-labelledby="budgets-heading">
      <h2 id="budgets-heading">Budgets</h2>
      <p className="muted">Paid research does not start without a budget, and stops when the budget is used up.</p>
      {budgets.isPending ? (
        <Loading label="Loading budgets" />
      ) : budgets.isError ? (
        <ErrorState error={budgets.error} onRetry={() => void budgets.refetch()} />
      ) : budgets.data.length === 0 ? (
        <EmptyState title="No budget is set." />
      ) : (
        <ul className="rows">
          {budgets.data.map((budget) => (
            <li key={budget.id} className="row">
              <div className="row-main">
                <span className="row-title">
                  {budget.scope} · {budget.period === "month" ? `month from ${budget.period_start}` : "total"}
                </span>
                <span className="muted">
                  Limit {formatMoney(budget.limit_amount, budget.currency)} · spent {formatMoney(budget.spent_amount, budget.currency)} ·
                  held {formatMoney(budget.reserved_amount, budget.currency)} · free{" "}
                  {formatMoney(budget.available_amount, budget.currency)}
                </span>
              </div>
              {budget.overrun ? <span className="badge badge-warn">Over the limit: new work is blocked</span> : null}
            </li>
          ))}
        </ul>
      )}
      {can("tenant.billing") ? (
        <form
          className="inline-form"
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate();
          }}
        >
          <div className="field">
            <label htmlFor={limitId}>Monthly research limit</label>
            <input id={limitId} required inputMode="decimal" pattern="[0-9]+([.][0-9]{1,4})?" value={limit} onChange={(e) => setLimit(e.target.value)} />
          </div>
          <button type="submit" className="button" disabled={save.isPending}>
            Set limit
          </button>
        </form>
      ) : null}
      {save.isError ? <ErrorState error={save.error} /> : null}
    </section>
  );
}

function Usage() {
  const usage = useTenantQuery(["usage-summary"], () => unwrap(api.GET("/api/v1/usage/summary")));
  return (
    <section className="panel" aria-labelledby="usage-heading">
      <h2 id="usage-heading">Usage this month</h2>
      {usage.isPending ? (
        <Loading label="Loading usage" />
      ) : usage.isError ? (
        <ErrorState error={usage.error} onRetry={() => void usage.refetch()} />
      ) : (
        <dl className="facts">
          <div>
            <dt>Confirmed by the provider</dt>
            <dd>{formatMoney(usage.data.verified_amount, usage.data.currency)}</dd>
          </div>
          <div>
            <dt>Estimated here (may differ from the bill)</dt>
            <dd>{formatMoney(usage.data.estimated_amount, usage.data.currency)}</dd>
          </div>
          <div>
            <dt>Held for work in progress</dt>
            <dd>{formatMoney(usage.data.reserved_amount, usage.data.currency)}</dd>
          </div>
          <div>
            <dt>Held, outcome unknown</dt>
            <dd>{formatMoney(usage.data.unknown_reserved_amount, usage.data.currency)}</dd>
          </div>
        </dl>
      )}
    </section>
  );
}

function Mailboxes() {
  const { tenantId, can, me } = useAuth();
  const queryClient = useQueryClient();
  const key = ["mailboxes"];
  const mailboxes = useTenantQuery(key, () => unwrap(api.GET("/api/v1/mailboxes")));
  const availability = useTenantQuery(["gmail-availability"], () => unwrap(api.GET("/api/v1/mailboxes/gmail/availability")));
  const refresh = () => queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, key) });
  const act = useMutation({
    mutationFn: ({ id, action }: { id: string; action: "sync" | "disconnect" }) =>
      action === "sync"
        ? unwrap(api.POST("/api/v1/mailboxes/{mailbox_id}/sync", { params: { path: { mailbox_id: id } } }))
        : unwrap(api.DELETE("/api/v1/mailboxes/{mailbox_id}", { params: { path: { mailbox_id: id } } })),
    onSettled: refresh,
  });
  const manage = can("integrations.manage");

  return (
    <section className="panel" aria-labelledby="mailbox-heading">
      <h2 id="mailbox-heading">Mailbox</h2>
      {mailboxes.isPending ? (
        <Loading label="Loading mailboxes" />
      ) : mailboxes.isError ? (
        <ErrorState error={mailboxes.error} onRetry={() => void mailboxes.refetch()} />
      ) : mailboxes.data.length === 0 ? (
        <EmptyState title="No mailbox connected." />
      ) : (
        <ul className="rows">
          {mailboxes.data.map((mailbox) => (
            <li key={mailbox.id} className="row">
              <div className="row-main">
                <span className="row-title">{mailbox.email_address}</span>
                <span className="muted">
                  Gmail · {mailbox.mode === "internal" ? "your own organisation" : mailbox.mode} · replies{" "}
                  {mailbox.can_read_replies ? "are read" : "cannot be read"} · last checked{" "}
                  {formatDateTime(mailbox.last_synced_at, me.active_tenant?.timezone)}
                </span>
                {mailbox.verification !== "verified_live" ? (
                  <span className="muted">Not yet confirmed against a real mailbox.</span>
                ) : null}
                {mailbox.problems.map((problem) => (
                  <span key={problem} className="badge badge-warn">
                    {problem}
                  </span>
                ))}
              </div>
              <span className={mailbox.healthy ? "badge tier-A" : "badge badge-warn"}>{mailbox.status}</span>
              {manage && mailbox.status !== "revoked" ? (
                <div className="row-actions">
                  <button type="button" className="button" disabled={act.isPending} onClick={() => act.mutate({ id: mailbox.id, action: "sync" })}>
                    Check now<span className="visually-hidden"> {mailbox.email_address}</span>
                  </button>
                  <button
                    type="button"
                    className="button"
                    disabled={act.isPending}
                    onClick={() => {
                      if (window.confirm(`Disconnect ${mailbox.email_address}? Conversations already stored are kept.`))
                        act.mutate({ id: mailbox.id, action: "disconnect" });
                    }}
                  >
                    Disconnect<span className="visually-hidden"> {mailbox.email_address}</span>
                  </button>
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {act.isError ? <ErrorState error={act.error} /> : null}
      {availability.data && !availability.data.available ? (
        <p className="notice notice-warn">Gmail cannot be connected here: {availability.data.reason}</p>
      ) : null}
      {manage && availability.data?.available ? (
        <p>
          {/* A full-page navigation: the browser is sent to Google and back. */}
          <a className="button" href="/api/v1/mailboxes/gmail/connect">
            Connect a {availability.data.internal_domain} Gmail mailbox
          </a>
        </p>
      ) : null}
    </section>
  );
}
