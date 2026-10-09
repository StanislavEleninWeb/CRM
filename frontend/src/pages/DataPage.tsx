import { useMutation } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";

import { api, unwrap } from "../api/client";
import { useAuth, useTenantQuery } from "../auth/AuthContext";
import { ErrorState, Loading } from "../components/States";
import { formatDateTime } from "../lib/format";

const RETENTION: [string, string][] = [
  ["email_content_days", "Email text is kept for (days)"],
  ["audit_days", "Security log is kept for (days)"],
  ["webhook_delivery_days", "Webhook delivery records (days)"],
  ["outbox_event_days", "Event records (days)"],
];

export function DataPage() {
  const { can } = useAuth();
  return (
    <>
      <h1>Data and access</h1>
      {can("tenant.settings") ? <Retention /> : null}
      {can("owners.manage") ? <SupportAccess /> : null}
      {can("owners.manage") ? <ExportAndDeletion /> : null}
    </>
  );
}

function Retention() {
  const retention = useTenantQuery(["retention"], () => unwrap(api.GET("/api/v1/retention")));
  const [draft, setDraft] = useState<Record<string, string>>({});
  const save = useMutation({
    mutationFn: () =>
      unwrap(api.PUT("/api/v1/retention", { body: Object.fromEntries(Object.entries(draft).map(([k, v]) => [k, Number(v)])) })),
    onSuccess: () => {
      setDraft({});
      return retention.refetch();
    },
  });
  if (retention.isPending) return <Loading label="Loading retention settings" />;
  if (retention.isError) return <ErrorState error={retention.error} />;
  const data = retention.data as unknown as Record<string, number> & { bounds: Record<string, [number, number]>; not_covered: string[] };
  return (
    <section className="panel" aria-labelledby="retention-heading">
      <h2 id="retention-heading">How long things are kept</h2>
      <form
        className="stack"
        aria-label="Retention"
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        {RETENTION.map(([key, label]) => (
          <div className="field" key={key}>
            <label htmlFor={`retention-${key}`}>
              {label} — between {data.bounds[key]?.[0]} and {data.bounds[key]?.[1]}
            </label>
            <input
              id={`retention-${key}`}
              type="number"
              min={data.bounds[key]?.[0]}
              max={data.bounds[key]?.[1]}
              value={draft[key] ?? String(data[key])}
              onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}
            />
          </div>
        ))}
        <button type="submit" className="button" disabled={save.isPending || Object.keys(draft).length === 0}>
          Save
        </button>
      </form>
      {save.isError ? <ErrorState error={save.error} /> : null}
      <h3>What these settings do not reach</h3>
      <ul>
        {data.not_covered.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
    </section>
  );
}

function SupportAccess() {
  const { me } = useAuth();
  const grants = useTenantQuery(["support-grants"], () => unwrap(api.GET("/api/v1/support-grants")));
  const [email, setEmail] = useState("");
  const [hours, setHours] = useState("24");
  const [reason, setReason] = useState("");
  const [mail, setMail] = useState(false);
  const ids = { email: useId(), hours: useId(), reason: useId(), mail: useId() };
  const create = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/support-grants", {
          body: { grantee_email: email, hours: Number(hours), reason, include_communications: mail },
        }),
      ),
    onSuccess: () => {
      setEmail("");
      setReason("");
      return grants.refetch();
    },
  });
  const revoke = useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE("/api/v1/support-grants/{grant_id}", { params: { path: { grant_id: id } } })),
    onSuccess: () => grants.refetch(),
  });
  const zone = me.active_tenant?.timezone;
  return (
    <section className="panel" aria-labelledby="support-heading">
      <h2 id="support-heading">Support access</h2>
      <p className="muted">
        Nobody outside this workspace can look at it unless you allow one named person, read-only, for up to 72 hours. Every use is
        recorded in the security log. Email conversations are excluded unless you include them.
      </p>
      {grants.isError ? <ErrorState error={grants.error} /> : null}
      <ul className="rows">
        {(grants.data ?? []).map((grant) => (
          <li key={grant.id} className="row">
            <div className="row-main">
              <span className="row-title">{grant.grantee_email}</span>
              <span className="muted">
                {grant.reason} · until {formatDateTime(grant.expires_at, zone)}
                {grant.include_communications ? " · includes email conversations" : ""}
                {grant.first_used_at ? ` · first used ${formatDateTime(grant.first_used_at, zone)}` : " · not used"}
              </span>
            </div>
            <span className={grant.active ? "badge badge-warn" : "badge"}>{grant.active ? "active" : "ended"}</span>
            {grant.active ? (
              <div className="row-actions">
                <button type="button" className="button" disabled={revoke.isPending} onClick={() => revoke.mutate(grant.id)}>
                  End now<span className="visually-hidden"> for {grant.grantee_email}</span>
                </button>
              </div>
            ) : null}
          </li>
        ))}
      </ul>
      <form
        className="stack"
        aria-label="Allow support access"
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <div className="field">
          <label htmlFor={ids.email}>Person&apos;s email</label>
          <input id={ids.email} type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </div>
        <div className="field">
          <label htmlFor={ids.hours}>Hours (1 to 72)</label>
          <input id={ids.hours} type="number" min={1} max={72} value={hours} onChange={(e) => setHours(e.target.value)} required />
        </div>
        <div className="field">
          <label htmlFor={ids.reason}>Why</label>
          <input id={ids.reason} value={reason} onChange={(e) => setReason(e.target.value)} required minLength={10} maxLength={500} />
        </div>
        <label htmlFor={ids.mail} className="check">
          <input id={ids.mail} type="checkbox" checked={mail} onChange={(e) => setMail(e.target.checked)} /> Include email conversations
        </label>
        <button type="submit" className="button" disabled={create.isPending}>
          Allow access
        </button>
      </form>
      {create.isError ? <ErrorState error={create.error} /> : null}
    </section>
  );
}

function ExportAndDeletion() {
  const { me } = useAuth();
  const deletion = useTenantQuery(["deletion"], () => unwrap(api.GET("/api/v1/tenant/deletion")));
  const [name, setName] = useState("");
  const nameId = useId();
  const request = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/tenant/deletion", { body: { confirm_name: name } })),
    onSuccess: () => deletion.refetch(),
  });
  const cancel = useMutation({
    mutationFn: () => unwrap(api.DELETE("/api/v1/tenant/deletion")),
    onSuccess: () => deletion.refetch(),
  });
  return (
    <section className="panel" aria-labelledby="export-heading">
      <h2 id="export-heading">Export and deletion</h2>
      <p>
        <a className="button" href="/api/v1/exports/workspace.zip">
          Download everything in this workspace
        </a>
      </p>
      <p className="muted">One file per kind of record. Stored credentials and keys are not included.</p>
      <h3>Delete this workspace</h3>
      {deletion.isError ? <ErrorState error={deletion.error} /> : null}
      <ul>
        {(deletion.data?.what_happens ?? []).map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
      {deletion.data?.due_at ? (
        <div className="notice notice-warn" role="status">
          <p>This workspace will be deleted on {formatDateTime(deletion.data.due_at, me.active_tenant?.timezone)}.</p>
          <button type="button" className="button" disabled={cancel.isPending} onClick={() => cancel.mutate()}>
            Cancel the deletion
          </button>
        </div>
      ) : (
        <form
          className="inline-form"
          aria-label="Delete this workspace"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            request.mutate();
          }}
        >
          <div className="field">
            <label htmlFor={nameId}>Type the workspace name to confirm</label>
            <input id={nameId} value={name} onChange={(e) => setName(e.target.value)} required autoComplete="off" />
          </div>
          <button type="submit" className="button" disabled={request.isPending || name !== me.active_tenant?.name}>
            Schedule deletion
          </button>
        </form>
      )}
      {request.isError ? <ErrorState error={request.error} /> : null}
      {cancel.isError ? <ErrorState error={cancel.error} /> : null}
    </section>
  );
}
