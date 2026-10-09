import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";
import { Link } from "react-router-dom";

import { api, ApiError, type Schemas, unwrap } from "../api/client";
import { tenantKey, useAuth, useTenantQuery } from "../auth/AuthContext";
import { ConversationMessages } from "../components/EmailPanel";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { formatDate, formatDateTime } from "../lib/format";

type Thread = Schemas["ThreadOut"];
type Suppression = Schemas["SuppressionOut"];
type Policy = Schemas["OutreachPolicyOut"];
type Register = { state: string; note?: string; name?: string; version?: string; obtained_at?: string; entry_count?: number };

export function EmailPage() {
  const { can } = useAuth();
  return (
    <>
      <h1>Email</h1>
      <MailboxSummary />
      <SendsToCheck />
      <RepliesToReview />
      <OutreachRules />
      {can("crm.read") ? <Suppressions /> : null}
    </>
  );
}

function MailboxSummary() {
  const { me } = useAuth();
  const mailboxes = useTenantQuery(["mailboxes"], () => unwrap(api.GET("/api/v1/mailboxes")));
  if (mailboxes.isPending) return <Loading label="Loading mailbox status" />;
  if (mailboxes.isError) return <ErrorState error={mailboxes.error} onRetry={() => void mailboxes.refetch()} />;
  const active = mailboxes.data.filter((mailbox) => mailbox.status !== "revoked");
  if (active.length === 0) {
    return (
      <p className="notice notice-warn">
        No mailbox is connected, so replies are not being read. <Link to="/integrations">Connect one under Integrations.</Link>
      </p>
    );
  }
  return (
    <>
      {active.map((mailbox) => (
        <p key={mailbox.id} className={mailbox.healthy ? "notice" : "notice notice-warn"}>
          {mailbox.email_address}: {mailbox.healthy ? "reading replies" : mailbox.problems.join(" ")} · last checked{" "}
          {formatDateTime(mailbox.last_synced_at, me.active_tenant?.timezone)}
        </p>
      ))}
    </>
  );
}

/** Messages that did not go out cleanly. An uncertain one is never sent again by the application. */
function SendsToCheck() {
  const { tenantId, can, me } = useAuth();
  const queryClient = useQueryClient();
  const key = ["send-intents", "attention"];
  const intents = useTenantQuery(key, () =>
    unwrap(api.GET("/api/v1/send-intents", { params: { query: { needs_attention: true } } })),
  );
  const resolve = useMutation({
    mutationFn: ({ id, sent, note }: { id: string; sent: boolean; note: string }) =>
      unwrap(api.POST("/api/v1/send-intents/{intent_id}/resolve", { params: { path: { intent_id: id } }, body: { sent, note } })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, key) }),
  });
  if (intents.isPending) return null;
  if (intents.isError) return <ErrorState error={intents.error} onRetry={() => void intents.refetch()} />;
  if (intents.data.length === 0) return null;

  function decide(id: string, sent: boolean, question: string) {
    const note = window.prompt(question);
    if (note) resolve.mutate({ id, sent, note });
  }

  return (
    <section className="panel" aria-labelledby="sends-heading">
      <h2 id="sends-heading">Sends to check</h2>
      <ul className="rows">
        {intents.data.map((intent) => (
          <li key={intent.id} className="row">
            <div className="row-main">
              <span className="row-title">
                {intent.subject ?? "(no subject)"} → {intent.to_address}
              </span>
              <span className="badge badge-warn">
                {intent.state === "unknown" ? "Not known whether it was sent" : intent.state === "failed" ? "Not sent" : "Stopped before sending"}
              </span>
              <span className="muted">
                {intent.state_reason} · requested for {formatDateTime(intent.scheduled_for, me.active_tenant?.timezone)}
              </span>
              {intent.state === "unknown" ? (
                <span className="muted">It will not be sent again automatically. Look in the mailbox&apos;s Sent folder.</span>
              ) : null}
            </div>
            {can("outreach.approve") ? (
              <div className="row-actions">
                {intent.state === "unknown" ? (
                  <>
                    <button type="button" className="button" disabled={resolve.isPending} onClick={() => decide(intent.id, true, "Where and when did you see it in the Sent folder?")}>
                      It is in Sent<span className="visually-hidden">: {intent.subject}</span>
                    </button>
                    <button type="button" className="button" disabled={resolve.isPending} onClick={() => decide(intent.id, false, "What did you check to confirm it was not sent?")}>
                      It was not sent<span className="visually-hidden">: {intent.subject}</span>
                    </button>
                  </>
                ) : (
                  <button type="button" className="button" disabled={resolve.isPending} onClick={() => decide(intent.id, false, "Note for the record (what will be done about it)")}>
                    Acknowledge<span className="visually-hidden">: {intent.subject}</span>
                  </button>
                )}
              </div>
            ) : null}
          </li>
        ))}
      </ul>
      {resolve.isError ? <ErrorState error={resolve.error} /> : null}
    </section>
  );
}

function RepliesToReview() {
  const { tenantId, can, me } = useAuth();
  const queryClient = useQueryClient();
  const key = ["email-threads", "review"];
  const threads = useTenantQuery(key, () =>
    unwrap(api.GET("/api/v1/email-threads", { params: { query: { needs_review: true } } })),
  );
  const refresh = () => queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, key) });
  const link = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Schemas["LinkThread"] }) =>
      unwrap(api.POST("/api/v1/email-threads/{thread_id}/link", { params: { path: { thread_id: id } }, body })),
    onSuccess: refresh,
  });

  return (
    <section className="panel" aria-labelledby="review-heading">
      <h2 id="review-heading">Messages to match</h2>
      <p className="muted">
        These could not be matched to exactly one prospect. Nothing is guessed: choose the prospect, or set the message aside.
      </p>
      {threads.isPending ? (
        <Loading label="Loading messages" />
      ) : threads.isError ? (
        <ErrorState error={threads.error} onRetry={() => void threads.refetch()} />
      ) : threads.data.items.length === 0 ? (
        <EmptyState title="Nothing waiting to be matched." />
      ) : (
        <ul className="rows">
          {threads.data.items.map((thread: Thread) => (
            <li key={thread.id} className="row">
              <div className="row-main">
                <span className="row-title">{thread.subject ?? "(no subject)"}</span>
                {thread.link_note ? <span className="badge badge-warn">{thread.link_note}</span> : null}
                <ConversationMessages thread={thread} timeZone={me.active_tenant?.timezone} />
                {can("crm.write") ? (
                  <ProspectPicker
                    label={thread.subject ?? "this message"}
                    disabled={link.isPending}
                    onPick={(leadId) => link.mutate({ id: thread.id, body: { lead_id: leadId } })}
                  />
                ) : null}
              </div>
              {can("crm.write") ? (
                <div className="row-actions">
                  <button
                    type="button"
                    className="button"
                    disabled={link.isPending}
                    onClick={() => link.mutate({ id: thread.id, body: { ignore: true } })}
                  >
                    Not a prospect<span className="visually-hidden">: {thread.subject ?? "this message"}</span>
                  </button>
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {link.isError ? <ErrorState error={link.error} /> : null}
    </section>
  );
}

function ProspectPicker({ label, disabled, onPick }: { label: string; disabled: boolean; onPick: (leadId: string) => void }) {
  const [term, setTerm] = useState("");
  const [submitted, setSubmitted] = useState("");
  const inputId = useId();
  const results = useTenantQuery(
    ["leads", "pick", submitted],
    () => unwrap(api.GET("/api/v1/leads", { params: { query: { q: submitted, limit: 5 } } })),
    { enabled: submitted.length >= 2 },
  );
  return (
    <div>
      <form
        className="inline-form"
        role="search"
        aria-label={`Find the prospect for ${label}`}
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          setSubmitted(term.trim());
        }}
      >
        <div className="field">
          <label htmlFor={inputId}>Prospect name</label>
          <input id={inputId} value={term} onChange={(e) => setTerm(e.target.value)} minLength={2} required />
        </div>
        <button type="submit" className="button">
          Find
        </button>
      </form>
      {results.isError ? <ErrorState error={results.error} /> : null}
      {results.data && results.data.items.length === 0 ? <p className="muted">No prospect matches.</p> : null}
      {(results.data?.items ?? []).map((lead) => (
        <button key={lead.id} type="button" className="button" disabled={disabled} onClick={() => onPick(lead.id)}>
          Match to {lead.company_name}
        </button>
      ))}
    </div>
  );
}

function OutreachRules() {
  const { tenantId, can } = useAuth();
  const queryClient = useQueryClient();
  const key = ["outreach-policy"];
  const policy = useTenantQuery(key, () => unwrap(api.GET("/api/v1/outreach-policy")));
  const refresh = () => queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, key) });

  return (
    <section className="panel" aria-labelledby="rules-heading">
      <h2 id="rules-heading">Outreach rules</h2>
      {policy.isPending ? (
        <Loading label="Loading rules" />
      ) : policy.isError ? (
        <ErrorState error={policy.error} onRetry={() => void policy.refetch()} />
      ) : (
        <RulesBody policy={policy.data} canSettings={can("tenant.settings")} canApprove={can("owners.manage")} onChanged={refresh} />
      )}
    </section>
  );
}

function RulesBody({
  policy,
  canSettings,
  canApprove,
  onChanged,
}: {
  policy: Policy;
  canSettings: boolean;
  canApprove: boolean;
  onChanged: () => Promise<void>;
}) {
  const register = policy.opt_out_register as Register;
  const approved = policy.status === "approved";
  return (
    <>
      <p className={approved ? "notice" : "notice notice-warn"}>
        {approved
          ? `Rules ${policy.version} were approved on ${formatDate(policy.approved_at)}.`
          : `Rules ${policy.version} are a draft. Email that the recipient has not asked for stays blocked until the owner approves them.`}
      </p>
      {policy.source_note ? <p className="muted">{policy.source_note}</p> : null}
      <dl className="facts">
        <dt>Sender identification</dt>
        <dd>{policy.sender_identity ?? "Not set"}</dd>
        <dt>Opt-out register</dt>
        <dd>
          {register.state === "unavailable"
            ? "None imported. A missing register is never treated as empty."
            : `${register.name ?? "Register"} ${register.version ?? ""}, ${register.entry_count ?? 0} addresses, obtained ${formatDate(register.obtained_at)} — ${
                register.state === "stale" ? "out of date" : "current"
              }`}
        </dd>
      </dl>
      {canSettings ? <SenderIdentityForm current={policy.sender_identity ?? ""} onDone={onChanged} /> : null}
      {canSettings ? <RegisterImport onDone={onChanged} /> : null}
      {canApprove ? <ApproveForm policy={policy} onDone={onChanged} /> : null}
    </>
  );
}

function SenderIdentityForm({ current, onDone }: { current: string; onDone: () => Promise<void> }) {
  const [value, setValue] = useState(current);
  const inputId = useId();
  const save = useMutation({
    mutationFn: () => unwrap(api.PUT("/api/v1/outreach-policy/sender-identity", { body: { sender_identity: value } })),
    onSuccess: onDone,
  });
  return (
    <form
      className="inline-form"
      aria-label="Sender identification"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <div className="field">
        <label htmlFor={inputId}>Company name and address shown in every outreach email</label>
        <input id={inputId} value={value} onChange={(e) => setValue(e.target.value)} required minLength={10} maxLength={500} />
      </div>
      <button type="submit" className="button" disabled={save.isPending || value === current}>
        Save
      </button>
      {save.isError ? <ErrorState error={save.error} /> : null}
    </form>
  );
}

function readCsrf(): string {
  const entry = document.cookie.split("; ").find((part) => part.startsWith("crm_csrf="));
  return entry ? decodeURIComponent(entry.slice("crm_csrf=".length)) : "";
}

function RegisterImport({ onDone }: { onDone: () => Promise<void> }) {
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [version, setVersion] = useState("");
  const [how, setHow] = useState("");
  const [validDays, setValidDays] = useState("7");
  const ids = { file: useId(), name: useId(), version: useId(), how: useId(), days: useId() };
  const upload = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error("Choose the register file first.");
      const body = new FormData();
      body.append("file", file);
      body.append("name", name);
      body.append("version", version);
      body.append("obtained_how", how);
      body.append("valid_days", validDays);
      // Multipart upload. The generated client is JSON-only, so this request is made by hand.
      const response = await fetch(`${window.location.origin}/api/v1/regulatory-sources`, {
        method: "POST",
        body,
        credentials: "same-origin",
        headers: { "X-CSRF-Token": readCsrf() },
      });
      const payload = await response.json().catch(() => undefined);
      if (!response.ok) throw new ApiError(response.status, payload);
    },
    onSuccess: () => {
      setFile(null);
      return onDone();
    },
  });
  return (
    <details>
      <summary>Import an opt-out register</summary>
      <p className="muted">
        Upload the register as obtained through its official channel, one address per line. The application does not fetch it.
      </p>
      <form
        className="stack"
        aria-label="Import an opt-out register"
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          upload.mutate();
        }}
      >
        <div className="field">
          <label htmlFor={ids.file}>Register file</label>
          <input id={ids.file} type="file" accept=".txt,.csv,text/plain" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </div>
        <div className="field">
          <label htmlFor={ids.name}>Register name</label>
          <input id={ids.name} value={name} onChange={(e) => setName(e.target.value)} required minLength={3} maxLength={200} />
        </div>
        <div className="field">
          <label htmlFor={ids.version}>Version or date of the register</label>
          <input id={ids.version} value={version} onChange={(e) => setVersion(e.target.value)} required maxLength={100} />
        </div>
        <div className="field">
          <label htmlFor={ids.how}>How it was obtained</label>
          <input id={ids.how} value={how} onChange={(e) => setHow(e.target.value)} required minLength={10} maxLength={500} />
        </div>
        <div className="field">
          <label htmlFor={ids.days}>Days it stays valid</label>
          <input id={ids.days} type="number" min={1} max={90} value={validDays} onChange={(e) => setValidDays(e.target.value)} required />
        </div>
        <button type="submit" className="button" disabled={upload.isPending || !file}>
          Import register
        </button>
      </form>
      {upload.isError ? <ErrorState error={upload.error} /> : null}
    </details>
  );
}

function ApproveForm({ policy, onDone }: { policy: Policy; onDone: () => Promise<void> }) {
  const rules = policy.rules as { label_text?: string; register_max_age_days?: number };
  const [note, setNote] = useState("");
  const [checkedOn, setCheckedOn] = useState("");
  const [label, setLabel] = useState(rules.label_text ?? "");
  const [maxAge, setMaxAge] = useState(String(rules.register_max_age_days ?? 7));
  const ids = { note: useId(), checked: useId(), label: useId(), age: useId() };
  const approve = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/outreach-policy/approve", {
          body: { approval_note: note, source_checked_on: checkedOn, label_text: label, register_max_age_days: Number(maxAge) },
        }),
      ),
    onSuccess: () => {
      setNote("");
      return onDone();
    },
  });
  return (
    <details>
      <summary>{policy.status === "approved" ? "Approve a new version of the rules" : "Approve the rules"}</summary>
      <p className="muted">
        Approving records that you, as owner, have had the current legal text and the register process reviewed. The application
        cannot do that review for you.
      </p>
      <form
        className="stack"
        aria-label="Approve the outreach rules"
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          approve.mutate();
        }}
      >
        <div className="field">
          <label htmlFor={ids.note}>Who reviewed the current law and register process, and when</label>
          <textarea id={ids.note} rows={3} value={note} onChange={(e) => setNote(e.target.value)} required minLength={20} maxLength={2000} />
        </div>
        <div className="field">
          <label htmlFor={ids.checked}>Date the legal source was checked</label>
          <input id={ids.checked} type="date" value={checkedOn} onChange={(e) => setCheckedOn(e.target.value)} required />
        </div>
        <div className="field">
          <label htmlFor={ids.label}>Label added to email the recipient has not asked for</label>
          <input id={ids.label} value={label} onChange={(e) => setLabel(e.target.value)} required maxLength={200} />
        </div>
        <div className="field">
          <label htmlFor={ids.age}>Oldest register that may be relied on, in days</label>
          <input id={ids.age} type="number" min={1} max={90} value={maxAge} onChange={(e) => setMaxAge(e.target.value)} required />
        </div>
        <button type="submit" className="button" disabled={approve.isPending}>
          Approve
        </button>
      </form>
      {approve.isError ? <ErrorState error={approve.error} /> : null}
    </details>
  );
}

function Suppressions() {
  const { tenantId, can } = useAuth();
  const queryClient = useQueryClient();
  const key = ["email-suppressions"];
  const list = useTenantQuery(key, () => unwrap(api.GET("/api/v1/email-suppressions")));
  const refresh = () => queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, key) });
  const [value, setValue] = useState("");
  const [scope, setScope] = useState<"address" | "domain">("address");
  const ids = { value: useId(), scope: useId() };
  const add = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/email-suppressions", { body: { value, scope, reason: "manual" } })),
    onSuccess: () => {
      setValue("");
      return refresh();
    },
  });
  const lift = useMutation({
    mutationFn: ({ id, note }: { id: string; note: string }) =>
      unwrap(
        api.POST("/api/v1/email-suppressions/{suppression_id}/lift", {
          params: { path: { suppression_id: id } },
          body: { lift_note: note },
        }),
      ),
    onSuccess: refresh,
  });

  return (
    <section className="panel" aria-labelledby="suppression-heading">
      <h2 id="suppression-heading">Do not email</h2>
      <p className="muted">Addresses and domains that are never emailed, whatever a later import contains.</p>
      {list.isPending ? (
        <Loading label="Loading the list" />
      ) : list.isError ? (
        <ErrorState error={list.error} onRetry={() => void list.refetch()} />
      ) : list.data.items.length === 0 ? (
        <EmptyState title="No address is blocked." />
      ) : (
        <ul className="rows">
          {list.data.items.map((item: Suppression) => (
            <li key={item.id} className="row">
              <div className="row-main">
                <span className="row-title">{item.value}</span>
                <span className="muted">
                  {item.reason.replace(/_/g, " ")} · added {formatDate(item.created_at)} · {item.source.replace(/_/g, " ")}
                  {item.lifted_at ? ` · lifted ${formatDate(item.lifted_at)}: ${item.lift_note ?? ""}` : ""}
                </span>
              </div>
              {can("outreach.approve") && !item.lifted_at ? (
                <div className="row-actions">
                  <button
                    type="button"
                    className="button"
                    disabled={lift.isPending}
                    onClick={() => {
                      const note = window.prompt(`Why may ${item.value} be emailed again?`);
                      if (note) lift.mutate({ id: item.id, note });
                    }}
                  >
                    Lift<span className="visually-hidden"> the block on {item.value}</span>
                  </button>
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {lift.isError ? <ErrorState error={lift.error} /> : null}
      {can("outreach.draft") ? (
        <form
          className="inline-form"
          aria-label="Block an address"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            add.mutate();
          }}
        >
          <div className="field">
            <label htmlFor={ids.scope}>Block</label>
            <select id={ids.scope} value={scope} onChange={(e) => setScope(e.target.value as typeof scope)}>
              <option value="address">One address</option>
              <option value="domain">A whole domain</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor={ids.value}>{scope === "address" ? "Email address" : "Domain"}</label>
            <input id={ids.value} value={value} onChange={(e) => setValue(e.target.value)} required maxLength={254} />
          </div>
          <button type="submit" className="button" disabled={add.isPending}>
            Block
          </button>
        </form>
      ) : null}
      {add.isError ? <ErrorState error={add.error} /> : null}
    </section>
  );
}
