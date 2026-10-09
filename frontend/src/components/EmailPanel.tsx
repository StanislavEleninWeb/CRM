import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";

import { api, type Schemas, unwrap } from "../api/client";
import { tenantKey, useAuth, useTenantQuery } from "../auth/AuthContext";
import { formatDateTime } from "../lib/format";
import { EmptyState, ErrorState, Loading } from "./States";

type Draft = Schemas["DraftOut"];
type Thread = Schemas["ThreadOut"];
type Reason = { level: string; code: string; message: string };

const KIND_LABELS: Record<string, string> = {
  unsolicited: "First contact (they have not asked for it)",
  requested: "Details they asked for",
  reply: "Reply",
};
const OUTCOME_LABELS: Record<string, string> = {
  allow: "Meets the outreach rules",
  review: "Needs a person to review",
  block: "May not be sent",
};

/** Conversations and drafts for one prospect. Nothing here sends an email. */
export function EmailPanel({ leadId }: { leadId: string }) {
  const { tenantId, can, me } = useAuth();
  const queryClient = useQueryClient();
  const threadsKey = ["email-threads", leadId];
  const draftsKey = ["email-drafts", leadId];
  const threads = useTenantQuery(threadsKey, () =>
    unwrap(api.GET("/api/v1/email-threads", { params: { query: { lead_id: leadId } } })),
  );
  const drafts = useTenantQuery(draftsKey, () =>
    unwrap(api.GET("/api/v1/email-drafts", { params: { query: { lead_id: leadId } } })),
  );
  const refreshDrafts = () => queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, draftsKey) });
  const refreshThreads = () => queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, threadsKey) });
  const [kind, setKind] = useState<"unsolicited" | "requested">("unsolicited");
  const kindId = useId();

  const start = useMutation({
    mutationFn: (body: Schemas["DraftIn"]) => unwrap(api.POST("/api/v1/email-drafts", { body })),
    onSuccess: refreshDrafts,
  });

  return (
    <section className="panel" aria-labelledby="email-heading">
      <h2 id="email-heading">Email</h2>
      {threads.isPending ? (
        <Loading label="Loading conversations" />
      ) : threads.isError ? (
        <ErrorState error={threads.error} onRetry={() => void threads.refetch()} />
      ) : threads.data.items.length === 0 ? (
        <EmptyState title="No email conversation with this prospect yet." />
      ) : (
        <ul className="rows">
          {threads.data.items.map((thread: Thread) => (
            <li key={thread.id} className="row">
              <div className="row-main">
                <span className="row-title">{thread.subject ?? "(no subject)"}</span>
                <ConversationMessages thread={thread} timeZone={me.active_tenant?.timezone} />
              </div>
              {thread.has_inbound ? (
                <div className="row-actions">
                  {can("crm.write") ? <ReplyOutcome thread={thread} onDone={refreshThreads} /> : null}
                </div>
              ) : null}
              {can("outreach.draft") && thread.has_inbound ? (
                <div className="row-actions">
                  <button
                    type="button"
                    className="button"
                    disabled={start.isPending}
                    onClick={() => start.mutate({ kind: "reply", thread_id: thread.id })}
                  >
                    Draft a reply<span className="visually-hidden"> to {thread.subject ?? "this conversation"}</span>
                  </button>
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      )}

      {drafts.isError ? <ErrorState error={drafts.error} onRetry={() => void drafts.refetch()} /> : null}
      {(drafts.data ?? []).map((draft: Draft) => (
        <DraftEditor key={`${draft.id}-${draft.version}`} draft={draft} onChanged={refreshDrafts} />
      ))}

      {can("outreach.draft") ? (
        <form
          className="inline-form"
          aria-label="Start an email draft"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            start.mutate({ kind, lead_id: leadId });
          }}
        >
          <div className="field">
            <label htmlFor={kindId}>Kind of email</label>
            <select id={kindId} value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
              <option value="unsolicited">{KIND_LABELS.unsolicited}</option>
              <option value="requested">{KIND_LABELS.requested}</option>
            </select>
          </div>
          <button type="submit" className="button" disabled={start.isPending}>
            Start a draft
          </button>
        </form>
      ) : null}
      {start.isError ? <ErrorState error={start.error} /> : null}
    </section>
  );
}

/** A person says how a reply reads. It is never worked out from the wording. */
function ReplyOutcome({ thread, onDone }: { thread: Thread; onDone: () => Promise<void> }) {
  const selectId = useId();
  const mark = useMutation({
    mutationFn: (outcome: "positive" | "neutral" | "negative" | null) =>
      unwrap(api.POST("/api/v1/email-threads/{thread_id}/reply-outcome", { params: { path: { thread_id: thread.id } }, body: { outcome } })),
    onSuccess: onDone,
  });
  return (
    <div className="field">
      <label htmlFor={selectId}>This reply is</label>
      <select
        id={selectId}
        value={thread.reply_outcome ?? ""}
        disabled={mark.isPending}
        onChange={(e) => mark.mutate((e.target.value || null) as "positive" | "neutral" | "negative" | null)}
      >
        <option value="">Not judged</option>
        <option value="positive">Positive</option>
        <option value="neutral">Neutral</option>
        <option value="negative">Negative</option>
      </select>
      {mark.isError ? <ErrorState error={mark.error} /> : null}
    </div>
  );
}

/** Message bodies are shown as plain text only; HTML from a mailbox is never rendered. */
export function ConversationMessages({ thread, timeZone }: { thread: Thread; timeZone?: string }) {
  return (
    <ol className="messages">
      {(thread.messages ?? []).map((message) => (
        <li key={message.id}>
          <span className="muted">
            {message.direction === "outbound" ? "Sent" : "Received"} {formatDateTime(message.sent_at, timeZone)}
            {message.direction === "inbound" && message.from_address ? ` · from ${message.from_address}` : ""}
            {message.classification === "out_of_office" ? " · automatic reply" : ""}
            {message.classification === "bounce" ? " · delivery failure" : ""}
            {message.classification === "auto_generated" ? " · automated message" : ""}
          </span>
          <p className="message-body">{message.body_text ?? message.snippet ?? ""}</p>
          {(message.attachments ?? []).length > 0 ? (
            <p className="muted">
              Attachments stay in the mailbox and are not downloaded here:{" "}
              {(message.attachments as { filename?: string }[]).map((a) => a.filename ?? "unnamed").join(", ")}
            </p>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

function DraftEditor({ draft, onChanged }: { draft: Draft; onChanged: () => Promise<void> }) {
  const { can } = useAuth();
  const [to, setTo] = useState(draft.to_address);
  const [subject, setSubject] = useState(draft.subject);
  const [body, setBody] = useState(draft.body_text);
  const ids = { to: useId(), subject: useId(), body: useId() };
  const editable = can("outreach.draft") && (draft.status === "draft" || draft.status === "approved");
  const dirty = to !== draft.to_address || subject !== draft.subject || body !== draft.body_text;
  const reasons = draft.eligibility.reasons as Reason[];

  const save = useMutation({
    mutationFn: () =>
      unwrap(
        api.PATCH("/api/v1/email-drafts/{draft_id}", {
          params: { path: { draft_id: draft.id } },
          body: { to_address: to, subject, body_text: body },
        }),
      ),
    onSuccess: onChanged,
  });
  const discard = useMutation({
    mutationFn: () => unwrap(api.DELETE("/api/v1/email-drafts/{draft_id}", { params: { path: { draft_id: draft.id } } })),
    onSuccess: onChanged,
  });

  return (
    <article className="draft" aria-label={`Draft to ${draft.to_address}`}>
      <h3>
        Draft · {KIND_LABELS[draft.kind] ?? draft.kind} · version {draft.version}
      </h3>
      <form
        className="stack"
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        <div className="field">
          <label htmlFor={ids.to}>To</label>
          <input id={ids.to} type="email" value={to} disabled={!editable} onChange={(e) => setTo(e.target.value)} required />
        </div>
        <div className="field">
          <label htmlFor={ids.subject}>Subject</label>
          <input id={ids.subject} value={subject} disabled={!editable} maxLength={300} onChange={(e) => setSubject(e.target.value)} required />
        </div>
        <div className="field">
          <label htmlFor={ids.body}>Message</label>
          <textarea id={ids.body} rows={8} value={body} disabled={!editable} onChange={(e) => setBody(e.target.value)} required />
        </div>
        {editable ? (
          <div className="row-actions">
            <button type="submit" className="button" disabled={!dirty || save.isPending}>
              Save and re-check
            </button>
            <button
              type="button"
              className="button"
              disabled={discard.isPending}
              onClick={() => {
                if (window.confirm("Discard this draft?")) discard.mutate();
              }}
            >
              Discard
            </button>
          </div>
        ) : null}
      </form>
      {save.isError ? <ErrorState error={save.error} /> : null}
      {discard.isError ? <ErrorState error={discard.error} /> : null}

      <div className={draft.eligibility.outcome === "allow" ? "notice" : "notice notice-warn"} role="status">
        <strong>{OUTCOME_LABELS[draft.eligibility.outcome] ?? draft.eligibility.outcome}</strong>
        {dirty ? <span> — save to check your changes</span> : null}
        {reasons.length > 0 ? (
          <ul>
            {reasons.map((reason) => (
              <li key={reason.code}>{reason.message}</li>
            ))}
          </ul>
        ) : null}
        {draft.eligibility.policy_version ? <p className="muted">Checked against rules {draft.eligibility.policy_version}</p> : null}
      </div>
      {reasons.some((reason) => reason.code === "recipient_unclassified") && can("research.review") ? (
        <ClassifyRecipient address={draft.to_address} onDone={onChanged} />
      ) : null}
      <details>
        <summary>Preview of what would be sent</summary>
        <pre className="email-preview">{draft.eligibility.rendered_body}</pre>
      </details>
      <SendControls draft={draft} dirty={dirty} onChanged={onChanged} />
    </article>
  );
}

const SEND_STATE_LABELS: Record<string, string> = {
  queued: "Waiting to be sent",
  claimed: "Being sent",
  dispatching: "Being sent",
  provider_accepted: "Accepted by the mailbox provider",
  unknown: "Not known whether it was sent",
  failed: "Not sent",
  blocked: "Stopped before sending",
  cancelled: "Cancelled",
  simulated: "Dry run finished: nothing was sent",
};

/** Approve, send now or at a chosen time, and cancel. Each is a separate, explicit step. */
function SendControls({ draft, dirty, onChanged }: { draft: Draft; dirty: boolean; onChanged: () => Promise<void> }) {
  const { can, me } = useAuth();
  const sending = useTenantQuery(["email-sending"], () => unwrap(api.GET("/api/v1/email-sending")));
  const [note, setNote] = useState("");
  const [when, setWhen] = useState("");
  const ids = { note: useId(), when: useId() };
  const path = { params: { path: { draft_id: draft.id } } };
  const approve = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/email-drafts/{draft_id}/approve", { ...path, body: { review_note: note || null } })),
    onSuccess: onChanged,
  });
  const send = useMutation({
    mutationFn: (scheduledLocal: string | null) =>
      unwrap(api.POST("/api/v1/email-drafts/{draft_id}/send", { ...path, body: { scheduled_local: scheduledLocal } })),
    onSuccess: onChanged,
  });
  const cancel = useMutation({
    mutationFn: (intentId: string) =>
      unwrap(api.POST("/api/v1/send-intents/{intent_id}/cancel", { params: { path: { intent_id: intentId } } })),
    onSuccess: onChanged,
  });
  const mode = sending.data?.mode;
  const latest = draft.send;
  const outcome = draft.eligibility.outcome;
  const zone = me.active_tenant?.timezone;

  return (
    <div className="stack">
      {latest ? (
        <p className={latest.needs_attention ? "notice notice-warn" : "muted"} role="status">
          {SEND_STATE_LABELS[latest.state] ?? latest.state}
          {latest.state === "queued" ? ` · due ${formatDateTime(latest.scheduled_for, zone)}` : ""}
          {latest.state === "provider_accepted"
            ? ` ${formatDateTime(latest.accepted_at, zone)}. ${
                latest.delivery_evidence === "replied"
                  ? "They replied."
                  : latest.delivery_evidence === "bounced"
                    ? "It was returned as undeliverable."
                    : "This is not a confirmation that it was delivered or read."
              }`
            : ""}
          {latest.state_reason && latest.state !== "provider_accepted" ? ` — ${latest.state_reason}` : ""}
          {latest.dry_run && latest.state !== "simulated" ? " (dry run)" : ""}
        </p>
      ) : null}
      {mode === "off" ? <p className="muted">Sending is switched off on this installation. Drafts can be written and approved.</p> : null}
      {mode === "dry_run" ? (
        <p className="muted">Dry run: a send request runs every check and stops before the mailbox. Nothing is sent.</p>
      ) : null}

      {draft.status === "draft" && can("outreach.approve") ? (
        <form
          className="inline-form"
          aria-label="Approve this message"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            approve.mutate();
          }}
        >
          {outcome === "review" ? (
            <div className="field">
              <label htmlFor={ids.note}>What you checked about this recipient</label>
              <input id={ids.note} value={note} onChange={(e) => setNote(e.target.value)} required minLength={10} maxLength={1000} />
            </div>
          ) : null}
          <button type="submit" className="button" disabled={dirty || outcome === "block" || approve.isPending}>
            Approve this version
          </button>
        </form>
      ) : null}
      {draft.status === "approved" ? (
        <p className="muted">
          Version {draft.approved_version} approved {formatDateTime(draft.approved_at, zone)}. Editing it withdraws the approval.
        </p>
      ) : null}
      {draft.status === "approved" && can("outreach.send") && mode && mode !== "off" ? (
        <form
          className="inline-form"
          aria-label="Send this message"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            send.mutate(when || null);
          }}
        >
          <div className="field">
            <label htmlFor={ids.when}>Send at ({zone ?? "workspace"} time; leave empty to send now)</label>
            <input id={ids.when} type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} />
          </div>
          <button type="submit" className="button" disabled={dirty || send.isPending}>
            {when ? "Schedule" : mode === "dry_run" ? "Run the dry run" : "Send now"}
          </button>
        </form>
      ) : null}
      {latest?.state === "queued" && can("outreach.send") ? (
        <div className="row-actions">
          <button type="button" className="button" disabled={cancel.isPending} onClick={() => cancel.mutate(latest.id)}>
            Cancel sending
          </button>
        </div>
      ) : null}
      {[approve, send, cancel].map((mutation, index) => (mutation.isError ? <ErrorState key={index} error={mutation.error} /> : null))}
    </div>
  );
}

function ClassifyRecipient({ address, onDone }: { address: string; onDone: () => Promise<void> }) {
  const [legalForm, setLegalForm] = useState<Schemas["ProfileIn"]["legal_form"]>("legal_person");
  const [context, setContext] = useState<Schemas["ProfileIn"]["context"]>("business");
  const [evidence, setEvidence] = useState("");
  const ids = { form: useId(), context: useId(), evidence: useId() };
  const record = useMutation({
    mutationFn: () =>
      unwrap(api.PUT("/api/v1/recipient-profiles", { body: { address, legal_form: legalForm, context, evidence } })),
    onSuccess: onDone,
  });
  return (
    <form
      className="inline-form"
      aria-label={`Record who ${address} belongs to`}
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        record.mutate();
      }}
    >
      <div className="field">
        <label htmlFor={ids.form}>Recipient is</label>
        <select id={ids.form} value={legalForm} onChange={(e) => setLegalForm(e.target.value as typeof legalForm)}>
          <option value="legal_person">A company or other legal person</option>
          <option value="sole_trader">A sole trader</option>
          <option value="natural_person">A private individual</option>
        </select>
      </div>
      <div className="field">
        <label htmlFor={ids.context}>Address is used for</label>
        <select id={ids.context} value={context} onChange={(e) => setContext(e.target.value as typeof context)}>
          <option value="business">Business</option>
          <option value="consumer">Private matters</option>
        </select>
      </div>
      <div className="field">
        <label htmlFor={ids.evidence}>What this is based on</label>
        <input id={ids.evidence} value={evidence} onChange={(e) => setEvidence(e.target.value)} required minLength={5} maxLength={1000} />
      </div>
      <button type="submit" className="button" disabled={record.isPending}>
        Record
      </button>
      {record.isError ? <ErrorState error={record.error} /> : null}
    </form>
  );
}
