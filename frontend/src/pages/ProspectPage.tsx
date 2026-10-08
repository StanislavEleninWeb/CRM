import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { api, type Schemas, unwrap } from "../api/client";
import { tenantKey, useAuth, useTenantQuery } from "../auth/AuthContext";
import { EmailPanel } from "../components/EmailPanel";
import { actionReason, TierBadge, VerificationBadges } from "../components/ProspectBits";
import { ErrorState, Loading } from "../components/States";
import { formatDate, formatDateTime, localInputToIso } from "../lib/format";

type Detail = Schemas["ProspectDetail"];
type Call = Schemas["CallAttemptOut"];
type Outcome = NonNullable<Call["outcome"]>;

const OUTCOMES: [Outcome, string][] = [
  ["no_answer", "No answer"],
  ["connected", "Connected"],
  ["follow_up_requested", "Follow-up requested"],
  ["wrong_number", "Wrong number"],
  ["not_interested", "Not interested"],
  ["voicemail", "Voicemail"],
  ["busy", "Busy"],
];
const COMPONENT_LABELS: Record<string, [string, number]> = {
  evidence: ["Evidence strength", 30],
  relevance: ["Relevance to our services", 25],
  value: ["Plausible business value", 20],
  reachability: ["Reachability", 15],
  activity: ["Current business activity", 10],
};

export function ProspectPage() {
  const { leadId = "" } = useParams();
  const [search] = useSearchParams();
  const navigate = useNavigate();
  const { tenantId, can, me } = useAuth();
  const queryClient = useQueryClient();
  const key = ["prospect", leadId];
  const prospect = useTenantQuery(key, () =>
    unwrap(api.GET("/api/v1/prospects/{lead_id}", { params: { path: { lead_id: leadId } } })),
  );
  const [pendingCall, setPendingCall] = useState<Call | null>(null);
  const timeZone = me.active_tenant?.timezone;
  const nextId = search.get("next");

  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, key) });
    await queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, ["call-queue"]) });
    await queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, ["prospects"]) });
  };
  const startCall = useMutation({
    mutationFn: (channelId: string) =>
      unwrap(
        api.POST("/api/v1/prospects/{lead_id}/calls", {
          params: { path: { lead_id: leadId } },
          body: { channel_id: channelId },
        }),
      ),
    onSuccess: (call) => {
      setPendingCall(call);
      // Opening the dialler only records that it was opened. The outcome is asked for below.
      if (call.dial_uri) window.location.href = call.dial_uri;
    },
  });

  if (prospect.isPending) return <Loading label="Loading prospect" />;
  if (prospect.isError) return <ErrorState error={prospect.error} onRetry={() => void prospect.refetch()} />;
  const p = prospect.data;
  const awaiting = pendingCall ?? p.calls.find((call) => call.outcome === null) ?? null;
  const phones = p.channels.filter((channel) => channel.kind === "phone");
  const others = p.channels.filter((channel) => channel.kind !== "phone");

  return (
    <>
      <p>
        <Link to="/prospects">← Prospects</Link>
        {nextId ? (
          <>
            {" · "}
            <Link to={`/prospects/${nextId}`}>Next in queue →</Link>
          </>
        ) : null}
      </p>
      <div className="page-header">
        <h1>{p.company_name}</h1>
        <TierBadge prospect={p} />
      </div>
      <p className="muted">
        {[p.business_type, p.city, p.external_id].filter(Boolean).join(" · ")} ·{" "}
        <Link to={`/companies/${p.company_id}`}>Company record</Link>
      </p>
      <div className="badges">
        <span className="badge">Status: {p.outreach_status.replace(/_/g, " ")}</span>
        <span className="badge">Confidence: {p.confidence}</span>
        <span className="badge">Checked {formatDate(p.checked_on)}</span>
        <VerificationBadges prospect={p} />
        {p.status === "disqualified" ? <span className="badge badge-warn">Dismissed</span> : null}
      </div>

      <section className="panel" aria-labelledby="call-heading">
        <h2 id="call-heading">Call</h2>
        {p.channel_instruction ? <p className="notice">{p.channel_instruction}</p> : null}
        {p.restriction_reasons ? (
          <p className="notice notice-warn" role="note">
            {p.restriction_reasons}
          </p>
        ) : null}
        {phones.length === 0 ? <p className="muted">No phone number was found for this business.</p> : null}
        <ul className="rows">
          {phones.map((phone) => (
            <li key={phone.id} className="row">
              <div className="row-main">
                <span className="row-title">{phone.raw_value}</span>
                <span className="muted">
                  {phone.purpose}
                  {phone.label ? ` · ${phone.label}` : ""}
                </span>
              </div>
              {phone.dial_uri && p.actions.call?.available ? (
                <button
                  type="button"
                  className="button button-primary"
                  disabled={startCall.isPending}
                  onClick={() => startCall.mutate(phone.id)}
                  aria-label={`Call ${phone.raw_value}`}
                >
                  Call
                </button>
              ) : (
                <span className="badge badge-warn">
                  {phone.purpose === "emergency"
                    ? "Emergency line: not for sales calls"
                    : phone.verification_state === "invalid"
                      ? "Wrong number"
                      : actionReason(p.actions.call?.reason) || "Do not call"}
                </span>
              )}
            </li>
          ))}
        </ul>
        {startCall.isError ? <ErrorState error={startCall.error} /> : null}
        {can("calls.log") && p.actions.call?.reason !== "prospect_inactive" ? (
          <OutcomeForm
            key={awaiting?.id ?? "manual"}
            leadId={leadId}
            call={awaiting}
            phones={phones}
            onDone={async () => {
              setPendingCall(null);
              await refresh();
              if (nextId) navigate(`/prospects/${nextId}`);
            }}
          />
        ) : null}
        <p className="muted">
          Email: {p.actions.email?.available ? "available" : actionReason(p.actions.email?.reason)}
          {others.length ? ` · ${others.map((channel) => channel.raw_value).join(" · ")}` : ""}
        </p>
      </section>

      <section className="panel" aria-labelledby="talk-heading">
        <h2 id="talk-heading">What to say</h2>
        <EditableText leadId={leadId} field="outreach_opening" label="Opening" value={p.outreach_opening} onSaved={refresh} />
        <EditableText
          leadId={leadId}
          field="discovery_question"
          label="Discovery question"
          value={p.discovery_question}
          onSaved={refresh}
        />
        <dl className="facts">
          <div>
            <dt>Recommended service</dt>
            <dd>{p.recommended_service_raw ?? p.service_category ?? "—"}</dd>
          </div>
          <div>
            <dt>Proposed benefit (not a measured result)</dt>
            <dd>{p.proposed_benefit ?? "—"}</dd>
          </div>
        </dl>
      </section>

      <section className="panel" aria-labelledby="evidence-heading">
        <h2 id="evidence-heading">Evidence</h2>
        <h3>Observed</h3>
        <p>{p.finding ?? <span className="muted">No finding recorded.</span>}</p>
        <p className="muted">
          {p.finding_state === "verified"
            ? "Verified by a team member"
            : p.finding_state === "contradicted"
              ? "Contradicted on re-check"
              : `Not verified · source: ${p.source_type === "user_import" ? "imported list" : (p.source_type ?? "unknown")}`}
          {p.evidence_url ? (
            <>
              {" · "}
              <a href={p.evidence_url} target="_blank" rel="noopener noreferrer">
                Open source page
              </a>
            </>
          ) : null}
        </p>
        {p.observation_id && can("research.review") ? (
          <VerifyButtons observationId={p.observation_id} onDone={refresh} />
        ) : null}
        <h3>Assumed, to confirm in conversation</h3>
        <p>{p.hypothesis ?? <span className="muted">No hypothesis recorded.</span>}</p>
        {p.hypothesis ? <p className="muted">Hypothesis: {p.hypothesis_status}</p> : null}
        <h3>Why it may fit</h3>
        <p>{p.fit_explanation ?? "—"}</p>
        <p className="muted">Website: {p.website_status_raw ?? "not assessed"}</p>
      </section>

      <ScoreSection prospect={p} />
      {can("crm.write") && p.status !== "disqualified" ? <OwnerAndConvert prospect={p} onDone={refresh} /> : null}
      {can("research.review") && p.status !== "disqualified" ? <DismissForm leadId={leadId} onDone={refresh} /> : null}

      <EmailPanel leadId={leadId} />

      <section className="panel" aria-labelledby="calls-heading">
        <h2 id="calls-heading">Call history</h2>
        {p.calls.length === 0 ? (
          <p className="muted">No calls logged.</p>
        ) : (
          <ul className="rows">
            {p.calls.map((call) => (
              <li key={call.id} className="row">
                <div className="row-main">
                  <span className="row-title">
                    {call.outcome ? call.outcome.replace(/_/g, " ") : "Dialler opened — no outcome reported"}
                  </span>
                  <span className="muted">
                    {call.dialed_value} · {formatDateTime(call.outcome_reported_at ?? call.launched_at, timeZone)}
                    {call.notes ? ` · ${call.notes}` : ""}
                  </span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}

function OutcomeForm({
  leadId,
  call,
  phones,
  onDone,
}: {
  leadId: string;
  call: Call | null;
  phones: Detail["channels"];
  onDone: () => Promise<void>;
}) {
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [notes, setNotes] = useState("");
  const [followUpAt, setFollowUpAt] = useState("");
  const [followUpNote, setFollowUpNote] = useState("");
  const [email, setEmail] = useState("");
  const [doNotCall, setDoNotCall] = useState(false);
  const [reason, setReason] = useState("");
  const ids = { notes: useId(), when: useId(), what: useId(), email: useId(), reason: useId() };
  const save = useMutation({
    mutationFn: () => {
      const body = {
        outcome: outcome as Outcome,
        notes: notes || null,
        follow_up_at: localInputToIso(followUpAt),
        follow_up_note: followUpNote || null,
        follow_up_scope: followUpNote || null,
        requested_email: email || null,
        do_not_call: doNotCall,
        do_not_call_reason: doNotCall ? reason : null,
      };
      return call
        ? unwrap(api.POST("/api/v1/calls/{call_id}/outcome", { params: { path: { call_id: call.id } }, body }))
        : unwrap(
            api.POST("/api/v1/prospects/{lead_id}/calls/manual", {
              params: { path: { lead_id: leadId } },
              body: { ...body, channel_id: phones[0]?.id ?? null, dialed_value: phones[0]?.raw_value ?? "unknown" },
            }),
          );
    },
    onSuccess: onDone,
  });
  const wantsFollowUp = outcome === "follow_up_requested" || outcome === "connected" || outcome === "no_answer";

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (outcome) save.mutate();
  }

  return (
    <form className="outcome" onSubmit={onSubmit} aria-label="Call outcome">
      <p>
        <strong>{call ? "What happened on the call?" : "Log a call made another way"}</strong>
        {call ? <span className="muted"> The dialler was opened; nothing is recorded as a call until you say.</span> : null}
      </p>
      <div className="choice-grid" role="group" aria-label="Outcome">
        {OUTCOMES.map(([value, label]) => (
          <button
            key={value}
            type="button"
            className={outcome === value ? "button button-primary" : "button"}
            aria-pressed={outcome === value}
            onClick={() => setOutcome(value)}
          >
            {label}
          </button>
        ))}
      </div>
      {outcome ? (
        <div className="stack">
          <div className="field">
            <label htmlFor={ids.notes}>Notes</label>
            <textarea id={ids.notes} rows={2} maxLength={5000} value={notes} onChange={(e) => setNotes(e.target.value)} />
          </div>
          {wantsFollowUp ? (
            <div className="grid-2">
              <div className="field">
                <label htmlFor={ids.when}>
                  Call back on{outcome === "follow_up_requested" ? "" : " (optional)"}
                </label>
                <input
                  id={ids.when}
                  type="datetime-local"
                  required={outcome === "follow_up_requested"}
                  value={followUpAt}
                  onChange={(e) => setFollowUpAt(e.target.value)}
                />
              </div>
              <div className="field">
                <label htmlFor={ids.what}>What they asked for</label>
                <input id={ids.what} maxLength={500} value={followUpNote} onChange={(e) => setFollowUpNote(e.target.value)} />
              </div>
            </div>
          ) : null}
          {outcome === "follow_up_requested" || outcome === "connected" ? (
            <div className="field">
              <label htmlFor={ids.email}>Business email they gave for this follow-up (optional)</label>
              <input id={ids.email} type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
            </div>
          ) : null}
          {outcome === "not_interested" ? (
            <>
              <label className="check">
                <input type="checkbox" checked={doNotCall} onChange={(e) => setDoNotCall(e.target.checked)} />
                They asked not to be called again
              </label>
              {doNotCall ? (
                <div className="field">
                  <label htmlFor={ids.reason}>What they said</label>
                  <input id={ids.reason} required maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
                </div>
              ) : null}
            </>
          ) : null}
          {save.isError ? <ErrorState error={save.error} /> : null}
          <button type="submit" className="button button-primary" disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save outcome"}
          </button>
        </div>
      ) : null}
    </form>
  );
}

function EditableText({
  leadId,
  field,
  label,
  value,
  onSaved,
}: {
  leadId: string;
  field: "outreach_opening" | "discovery_question";
  label: string;
  value: string | null | undefined;
  onSaved: () => Promise<void>;
}) {
  const { can } = useAuth();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? "");
  const id = useId();
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        api.PATCH("/api/v1/prospects/{lead_id}/assessment", {
          params: { path: { lead_id: leadId } },
          body: { [field]: draft },
        }),
      ),
    onSuccess: async () => {
      setEditing(false);
      await onSaved();
    },
  });
  if (!editing) {
    return (
      <div className="editable">
        <h3>{label}</h3>
        <p lang="bg">{value ?? <span className="muted">Not written yet.</span>}</p>
        {can("research.review") ? (
          <button type="button" className="button button-quiet" onClick={() => setEditing(true)}>
            Edit<span className="visually-hidden"> {label.toLowerCase()}</span>
          </button>
        ) : null}
      </div>
    );
  }
  return (
    <form
      className="stack"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <div className="field">
        <label htmlFor={id}>{label}</label>
        <textarea id={id} rows={4} value={draft} onChange={(e) => setDraft(e.target.value)} />
      </div>
      {save.isError ? <ErrorState error={save.error} /> : null}
      <div className="row-actions">
        <button type="submit" className="button button-primary" disabled={save.isPending}>
          Save
        </button>
        <button type="button" className="button" onClick={() => setEditing(false)}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function VerifyButtons({ observationId, onDone }: { observationId: string; onDone: () => Promise<void> }) {
  const [contradicting, setContradicting] = useState(false);
  const [note, setNote] = useState("");
  const noteId = useId();
  const verify = useMutation({
    mutationFn: (state: "verified" | "contradicted") =>
      unwrap(
        api.POST("/api/v1/observations/{observation_id}/verify", {
          params: { path: { observation_id: observationId } },
          body: { state, note: note || null },
        }),
      ),
    onSuccess: async () => {
      setContradicting(false);
      await onDone();
    },
  });
  return (
    <div className="stack">
      <div className="row-actions">
        <button type="button" className="button" disabled={verify.isPending} onClick={() => verify.mutate("verified")}>
          I checked: still true
        </button>
        <button type="button" className="button" onClick={() => setContradicting(true)}>
          No longer true
        </button>
      </div>
      {contradicting ? (
        <form
          className="inline-form"
          onSubmit={(event) => {
            event.preventDefault();
            verify.mutate("contradicted");
          }}
        >
          <div className="field">
            <label htmlFor={noteId}>What did you find instead?</label>
            <input id={noteId} required value={note} onChange={(e) => setNote(e.target.value)} />
          </div>
          <button type="submit" className="button" disabled={verify.isPending}>
            Save
          </button>
        </form>
      ) : null}
      {verify.isError ? <ErrorState error={verify.error} /> : null}
    </div>
  );
}

function ScoreSection({ prospect }: { prospect: Detail }) {
  const components = prospect.components;
  return (
    <section className="panel" aria-labelledby="score-heading">
      <h2 id="score-heading">Priority score</h2>
      {components ? (
        <>
          <dl className="facts">
            {Object.entries(COMPONENT_LABELS).map(([key, [label, max]]) => (
              <div key={key}>
                <dt>{label}</dt>
                <dd>
                  {components[key] ?? "—"} / {max}
                </dd>
              </div>
            ))}
            <div>
              <dt>Total</dt>
              <dd>
                {prospect.total} / 100 · Tier {prospect.tier}
              </dd>
            </div>
          </dl>
          <p className="muted">
            {prospect.score_origin === "override" ? "Set by a reviewer. " : ""}
            The score ranks prospects. Confidence is rated separately. {prospect.scores.length} version
            {prospect.scores.length === 1 ? "" : "s"} recorded.
          </p>
        </>
      ) : (
        <p className="muted">Not scored yet. An unscored prospect is not the same as a score of zero.</p>
      )}
    </section>
  );
}

function DismissForm({ leadId, onDone }: { leadId: string; onDone: () => Promise<void> }) {
  const [reason, setReason] = useState("");
  const id = useId();
  const dismiss = useMutation({
    mutationFn: () =>
      unwrap(api.POST("/api/v1/prospects/{lead_id}/dismiss", { params: { path: { lead_id: leadId } }, body: { reason } })),
    onSuccess: onDone,
  });
  return (
    <details className="panel">
      <summary>Dismiss this prospect</summary>
      <form
        className="inline-form"
        onSubmit={(event) => {
          event.preventDefault();
          dismiss.mutate();
        }}
      >
        <div className="field">
          <label htmlFor={id}>Reason</label>
          <input id={id} required minLength={2} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
        </div>
        <button type="submit" className="button" disabled={dismiss.isPending}>
          Dismiss
        </button>
      </form>
      {dismiss.isError ? <ErrorState error={dismiss.error} /> : null}
    </details>
  );
}

function OwnerAndConvert({ prospect, onDone }: { prospect: Detail; onDone: () => Promise<void> }) {
  const { can, me } = useAuth();
  const navigate = useNavigate();
  const members = useTenantQuery(["members"], () => unwrap(api.GET("/api/v1/members", { params: { query: { limit: 200 } } })));
  const [title, setTitle] = useState(prospect.recommended_service_raw ?? prospect.service_category ?? "");
  const [amount, setAmount] = useState("");
  const ids = { owner: useId(), title: useId(), amount: useId() };
  const assign = useMutation({
    mutationFn: (ownerId: string) =>
      unwrap(
        api.PATCH("/api/v1/prospects/{lead_id}/owner", {
          params: { path: { lead_id: prospect.lead_id }, query: ownerId ? { owner_user_id: ownerId } : {} },
        }),
      ),
    onSuccess: onDone,
  });
  const convert = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/leads/{lead_id}/convert", {
          params: { path: { lead_id: prospect.lead_id } },
          body: { title, amount: amount || null },
        }),
      ),
    onSuccess: async () => {
      await onDone();
      navigate("/opportunities");
    },
  });
  // A representative may take a prospect; handing it to someone else needs the assign permission.
  const options = (members.data?.items ?? []).filter((member) => can("crm.assign") || member.user_id === me.user.id);

  return (
    <section className="panel" aria-labelledby="next-steps-heading">
      <h2 id="next-steps-heading">Owner and next step</h2>
      <div className="field">
        <label htmlFor={ids.owner}>Owner</label>
        <select
          id={ids.owner}
          value={prospect.owner_user_id ?? ""}
          disabled={assign.isPending || members.isPending}
          onChange={(event) => assign.mutate(event.target.value)}
        >
          <option value="">Unassigned</option>
          {prospect.owner_user_id && !options.some((m) => m.user_id === prospect.owner_user_id) ? (
            <option value={prospect.owner_user_id}>Assigned to a colleague</option>
          ) : null}
          {options.map((member) => (
            <option key={member.user_id} value={member.user_id}>
              {member.user_id === me.user.id ? "Me" : member.display_name || member.email}
            </option>
          ))}
        </select>
      </div>
      {assign.isError ? <ErrorState error={assign.error} /> : null}
      {prospect.status === "converted" ? (
        <p>
          This prospect became an opportunity. <Link to="/opportunities">Open opportunities</Link>
        </p>
      ) : (
        <form
          className="inline-form"
          aria-label="Convert to opportunity"
          onSubmit={(event) => {
            event.preventDefault();
            convert.mutate();
          }}
        >
          <div className="field">
            <label htmlFor={ids.title}>Opportunity</label>
            <input id={ids.title} required maxLength={300} value={title} onChange={(e) => setTitle(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor={ids.amount}>Expected value ({me.active_tenant?.currency}, optional)</label>
            <input
              id={ids.amount}
              inputMode="decimal"
              pattern="[0-9]+([.][0-9]{1,2})?"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
            />
          </div>
          <button type="submit" className="button" disabled={convert.isPending}>
            Convert to opportunity
          </button>
        </form>
      )}
      {convert.isError ? <ErrorState error={convert.error} /> : null}
    </section>
  );
}
