import { useMutation } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { api, type Schemas, unwrap } from "../api/client";
import { useAuth, useTenantQuery } from "../auth/AuthContext";
import { ProspectCard } from "../components/ProspectBits";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { formatDate } from "../lib/format";

export function TodayPage() {
  const { me, can } = useAuth();
  const timeZone = me.active_tenant?.timezone;
  const queue = useTenantQuery(["call-queue", "today"], () => unwrap(api.GET("/api/v1/call-queue")));
  const pending = useTenantQuery(["prospects", { needs_verification: true, limit: 1 }], () =>
    unwrap(api.GET("/api/v1/prospects", { params: { query: { needs_verification: true, limit: 1 } } })),
  );
  const snapshot = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/shortlists", { body: { kind: "call_queue", day: "today" } })),
  });

  return (
    <>
      <div className="page-header">
        <h1>Today</h1>
        <span className="muted">
          {me.active_tenant?.name}
          {queue.data ? ` · ${formatDate(queue.data.queue_date)} (${queue.data.timezone})` : ""}
        </span>
      </div>

      <section aria-labelledby="queue-heading">
        <h2 id="queue-heading">Call queue</h2>
        {queue.isPending ? (
          <Loading label="Loading call queue" />
        ) : queue.isError ? (
          <ErrorState error={queue.error} onRetry={() => void queue.refetch()} />
        ) : queue.data.entries.length === 0 ? (
          <EmptyState title="Nobody to call yet.">
            <p>
              <Link to="/import">Import a prospect list</Link> to fill the queue. Only numbers that may be used for
              sales calls appear here.
            </p>
          </EmptyState>
        ) : (
          <>
            <p className="muted">
              Due follow-ups first, then phone-first prospects by score. Ties: {queue.data.tie_break}.
            </p>
            <details>
              <summary>How the queue moves on</summary>
              <ul>
                {queue.data.rules.map((rule) => (
                  <li key={rule}>{rule}</li>
                ))}
              </ul>
            </details>
            {queue.data.shortfall_reason ? (
              <p className="notice notice-warn" role="note">
                {queue.data.shortfall_reason}
              </p>
            ) : null}
            <ul className="prospect-cards">
              {queue.data.entries.map((entry, index) => (
                <QueueCard key={entry.prospect.lead_id} entry={entry} next={queue.data.entries[index + 1]} timeZone={timeZone} />
              ))}
            </ul>
            {can("research.review") ? (
              <p>
                <button type="button" className="button" disabled={snapshot.isPending} onClick={() => snapshot.mutate()}>
                  Save today’s list as a dated snapshot
                </button>{" "}
                {snapshot.isSuccess ? <span role="status">Saved: {snapshot.data.name}</span> : null}
                {snapshot.isError ? <ErrorState error={snapshot.error} /> : null}
              </p>
            ) : null}
          </>
        )}
      </section>

      <section className="panel" aria-labelledby="verify-heading">
        <h2 id="verify-heading">Waiting for verification</h2>
        {pending.isPending ? (
          <Loading label="Counting" />
        ) : pending.isError ? (
          <ErrorState error={pending.error} onRetry={() => void pending.refetch()} />
        ) : (
          <p>
            <strong>{pending.data.total}</strong> prospects have evidence that is unverified, old or low-confidence.{" "}
            {pending.data.total > 0 ? <Link to="/verification">Review them</Link> : null}
          </p>
        )}
      </section>

      <section className="panel" aria-labelledby="email-heading">
        <h2 id="email-heading">Email</h2>
        <p className="muted">No mailbox is connected. Calling and follow-ups work without one.</p>
      </section>
    </>
  );
}

type Entry = Schemas["ShortlistEntryOut"];

function QueueCard({ entry, next, timeZone }: { entry: Entry; next?: Entry; timeZone?: string }) {
  return (
    <div className="queue-item">
      <ProspectCard
        prospect={entry.prospect}
        rank={entry.rank}
        filler={entry.is_filler}
        reason={entry.reason}
        timeZone={timeZone}
      />
      <Link
        className="button button-primary"
        to={`/prospects/${entry.prospect.lead_id}${next ? `?next=${next.prospect.lead_id}` : ""}`}
      >
        Open<span className="visually-hidden"> {entry.prospect.company_name}</span>
      </Link>
    </div>
  );
}
