import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";

import { api, type Schemas, unwrap } from "../api/client";
import { tenantKey, useAuth, useTenantQuery } from "../auth/AuthContext";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { formatDateTime } from "../lib/format";

/** A value the server shows once. It lives only in this component's memory. */
function ShownOnce({ label, value, onDone }: { label: string; value: string; onDone: () => void }) {
  return (
    <div className="notice" role="status">
      <p>
        <strong>{label}</strong> Copy it now. It is not stored in a readable form and cannot be shown again.
      </p>
      <pre className="email-preview">{value}</pre>
      <button type="button" className="button" onClick={onDone}>
        I have copied it
      </button>
    </div>
  );
}

export function ApiKeys() {
  const { tenantId, me } = useAuth();
  const queryClient = useQueryClient();
  const key = ["api-keys"];
  const keys = useTenantQuery(key, () => unwrap(api.GET("/api/v1/api-keys")));
  const scopes = useTenantQuery(["api-key-scopes"], () => unwrap(api.GET("/api/v1/api-keys/scopes")));
  const refresh = () => queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, key) });
  const [name, setName] = useState("");
  const [chosen, setChosen] = useState<string[]>(["crm.read"]);
  const [days, setDays] = useState("90");
  const [fresh, setFresh] = useState<string | null>(null);
  const ids = { name: useId(), days: useId() };
  const zone = me.active_tenant?.timezone;

  const create = useMutation({
    mutationFn: () =>
      unwrap(api.POST("/api/v1/api-keys", { body: { name, scopes: chosen, expires_in_days: days ? Number(days) : null } })),
    onSuccess: (created) => {
      setFresh(created.key);
      setName("");
      return refresh();
    },
  });
  const revoke = useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE("/api/v1/api-keys/{key_id}", { params: { path: { key_id: id } } })),
    onSuccess: refresh,
  });

  return (
    <section className="panel" aria-labelledby="keys-heading">
      <h2 id="keys-heading">API keys</h2>
      <p className="muted">
        A key acts as you inside this workspace, limited to what you tick below. It can never approve an email, change settings or
        members, or read stored credentials.
      </p>
      {fresh ? <ShownOnce label="New API key." value={fresh} onDone={() => setFresh(null)} /> : null}
      {keys.isPending ? (
        <Loading label="Loading keys" />
      ) : keys.isError ? (
        <ErrorState error={keys.error} onRetry={() => void keys.refetch()} />
      ) : keys.data.length === 0 ? (
        <EmptyState title="No API keys." />
      ) : (
        <ul className="rows">
          {keys.data.map((item: Schemas["ApiKeyOut"]) => (
            <li key={item.id} className="row">
              <div className="row-main">
                <span className="row-title">{item.name}</span>
                <span className="muted">
                  {item.prefix}… · {item.scopes.join(", ")}
                </span>
                <span className="muted">
                  Last used {formatDateTime(item.last_used_at, zone)} · {item.expires_at ? `expires ${formatDateTime(item.expires_at, zone)}` : "does not expire"}
                </span>
              </div>
              <span className={item.usable ? "badge tier-A" : "badge badge-warn"}>
                {item.usable ? "active" : item.revoked_at ? "revoked" : "expired"}
              </span>
              {item.revoked_at ? null : (
                <div className="row-actions">
                  <button
                    type="button"
                    className="button"
                    disabled={revoke.isPending}
                    onClick={() => {
                      if (window.confirm(`Revoke ${item.name}? Anything using it stops working at once.`)) revoke.mutate(item.id);
                    }}
                  >
                    Revoke<span className="visually-hidden"> {item.name}</span>
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      {revoke.isError ? <ErrorState error={revoke.error} /> : null}
      <form
        className="stack"
        aria-label="Create an API key"
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <div className="field">
          <label htmlFor={ids.name}>What the key is for</label>
          <input id={ids.name} value={name} onChange={(e) => setName(e.target.value)} required maxLength={120} />
        </div>
        <fieldset>
          <legend>May do</legend>
          {(scopes.data ?? []).map((scope) => (
            <label key={scope.name} className="check">
              <input
                type="checkbox"
                checked={chosen.includes(scope.name)}
                disabled={!scope.grantable}
                onChange={(e) => setChosen(e.target.checked ? [...chosen, scope.name] : chosen.filter((s) => s !== scope.name))}
              />{" "}
              {scope.name}
            </label>
          ))}
        </fieldset>
        <div className="field">
          <label htmlFor={ids.days}>Expires after (days; empty for never)</label>
          <input id={ids.days} type="number" min={1} max={730} value={days} onChange={(e) => setDays(e.target.value)} />
        </div>
        <button type="submit" className="button" disabled={create.isPending || chosen.length === 0}>
          Create key
        </button>
      </form>
      {create.isError ? <ErrorState error={create.error} /> : null}
    </section>
  );
}

export function Webhooks() {
  const { tenantId, me } = useAuth();
  const queryClient = useQueryClient();
  const endpointsKey = ["webhook-endpoints"];
  const deliveriesKey = ["webhook-deliveries"];
  const endpoints = useTenantQuery(endpointsKey, () => unwrap(api.GET("/api/v1/webhook-endpoints")));
  const deliveries = useTenantQuery(deliveriesKey, () =>
    unwrap(api.GET("/api/v1/webhook-deliveries", { params: { query: { limit: 20 } } })),
  );
  const refresh = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, endpointsKey) }),
      queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, deliveriesKey) }),
    ]);
  const [url, setUrl] = useState("");
  const [secret, setSecret] = useState<string | null>(null);
  const urlId = useId();
  const zone = me.active_tenant?.timezone;

  const create = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/webhook-endpoints", { body: { url } })),
    onSuccess: (created) => {
      setSecret(created.secret);
      setUrl("");
      return refresh();
    },
  });
  const act = useMutation({
    mutationFn: async ({ id, action }: { id: string; action: "test" | "rotate" | "pause" | "resume" | "delete" }) => {
      const params = { params: { path: { endpoint_id: id } } };
      if (action === "test") return unwrap(api.POST("/api/v1/webhook-endpoints/{endpoint_id}/test", params));
      if (action === "delete") return unwrap(api.DELETE("/api/v1/webhook-endpoints/{endpoint_id}", params));
      if (action === "rotate") {
        const rotated = await unwrap(api.POST("/api/v1/webhook-endpoints/{endpoint_id}/rotate-secret", params));
        setSecret(rotated.secret);
        return rotated;
      }
      return unwrap(
        api.PATCH("/api/v1/webhook-endpoints/{endpoint_id}", { ...params, body: { status: action === "pause" ? "paused" : "active" } }),
      );
    },
    onSuccess: refresh,
  });
  const replay = useMutation({
    mutationFn: (id: string) =>
      unwrap(api.POST("/api/v1/webhook-deliveries/{delivery_id}/replay", { params: { path: { delivery_id: id } } })),
    onSuccess: refresh,
  });

  return (
    <section className="panel" aria-labelledby="webhooks-heading">
      <h2 id="webhooks-heading">Webhooks</h2>
      <p className="muted">
        Events are sent to a public HTTPS address, signed with a secret. The same event can arrive more than once; the receiver
        should ignore an event ID it has already handled.
      </p>
      {secret ? <ShownOnce label="Signing secret." value={secret} onDone={() => setSecret(null)} /> : null}
      {endpoints.isPending ? (
        <Loading label="Loading webhooks" />
      ) : endpoints.isError ? (
        <ErrorState error={endpoints.error} onRetry={() => void endpoints.refetch()} />
      ) : endpoints.data.length === 0 ? (
        <EmptyState title="No webhook addresses." />
      ) : (
        <ul className="rows">
          {endpoints.data.map((item: Schemas["EndpointOut"]) => (
            <li key={item.id} className="row">
              <div className="row-main">
                <span className="row-title">{item.url}</span>
                <span className="muted">
                  {item.event_types.includes("*") ? "All events" : item.event_types.join(", ")} · last delivered{" "}
                  {formatDateTime(item.last_success_at, zone)}
                </span>
                {item.consecutive_failures > 0 ? (
                  <span className="badge badge-warn">
                    {item.consecutive_failures} failed in a row: {item.last_error}
                  </span>
                ) : null}
                {item.old_signature_until ? (
                  <span className="muted">Also signed with the earlier secret until {formatDateTime(item.old_signature_until, zone)}</span>
                ) : null}
              </div>
              <span className={item.status === "active" ? "badge tier-A" : "badge badge-warn"}>{item.status}</span>
              <div className="row-actions">
                {(
                  [
                    ["test", "Send a test"],
                    [item.status === "active" ? "pause" : "resume", item.status === "active" ? "Pause" : "Resume"],
                    ["rotate", "New secret"],
                    ["delete", "Remove"],
                  ] as const
                ).map(([action, label]) => (
                  <button
                    key={action}
                    type="button"
                    className="button"
                    disabled={act.isPending}
                    onClick={() => {
                      if (action !== "delete" || window.confirm(`Remove ${item.url}? Undelivered events for it are dropped.`))
                        act.mutate({ id: item.id, action });
                    }}
                  >
                    {label}
                    <span className="visually-hidden"> {item.url}</span>
                  </button>
                ))}
              </div>
            </li>
          ))}
        </ul>
      )}
      {act.isError ? <ErrorState error={act.error} /> : null}
      <form
        className="inline-form"
        aria-label="Add a webhook address"
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <div className="field">
          <label htmlFor={urlId}>HTTPS address</label>
          <input id={urlId} type="url" value={url} onChange={(e) => setUrl(e.target.value)} required placeholder="https://" />
        </div>
        <button type="submit" className="button" disabled={create.isPending}>
          Add
        </button>
      </form>
      {create.isError ? <ErrorState error={create.error} /> : null}

      <h3>Recent deliveries</h3>
      {deliveries.isError ? <ErrorState error={deliveries.error} /> : null}
      {deliveries.data && deliveries.data.items.length === 0 ? <p className="muted">Nothing has been sent yet.</p> : null}
      <ul className="rows">
        {(deliveries.data?.items ?? []).map((item) => (
          <li key={item.id} className="row">
            <div className="row-main">
              <span className="row-title">{item.event_type}</span>
              <span className="muted">
                {item.status === "delivered"
                  ? `Delivered ${formatDateTime(item.delivered_at, zone)}`
                  : item.status === "dead"
                    ? `Gave up after ${item.attempts} attempts: ${item.last_error ?? ""}`
                    : `Attempt ${item.attempts} failed${item.last_error ? `: ${item.last_error}` : ""}; next try ${formatDateTime(item.next_attempt_at, zone)}`}
              </span>
            </div>
            <span className={item.status === "delivered" ? "badge tier-A" : "badge badge-warn"}>{item.status}</span>
            {item.status !== "pending" ? (
              <div className="row-actions">
                <button type="button" className="button" disabled={replay.isPending} onClick={() => replay.mutate(item.id)}>
                  Send again<span className="visually-hidden"> {item.event_type}</span>
                </button>
              </div>
            ) : null}
          </li>
        ))}
      </ul>
      {replay.isError ? <ErrorState error={replay.error} /> : null}
    </section>
  );
}
