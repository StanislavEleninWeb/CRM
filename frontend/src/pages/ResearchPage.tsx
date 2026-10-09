import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";
import { Link } from "react-router-dom";

import { api, type Schemas, unwrap } from "../api/client";
import { tenantKey, useAuth, useTenantQuery } from "../auth/AuthContext";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { formatDateTime, formatMoney } from "../lib/format";

type Run = Schemas["RunOut"];
type Candidate = Schemas["CandidateOut"];
const ACTIVE = new Set(["queued", "running", "cancelling"]);

export function ResearchPage() {
  const { can } = useAuth();
  return (
    <>
      <h1>Research</h1>
      <p className="muted">
        A run searches the cities and categories you choose, reads each business&apos;s own website, and proposes
        candidates. Nothing becomes a lead until a person accepts it. A run can finish with fewer candidates than you
        asked for; it is never padded.
      </p>
      <Configs canRun={can("research.run")} />
      <Runs canRun={can("research.run")} />
      <ReviewQueue canReview={can("research.review")} />
    </>
  );
}

function Configs({ canRun }: { canRun: boolean }) {
  const { tenantId, can } = useAuth();
  const queryClient = useQueryClient();
  const configs = useTenantQuery(["research-configs"], () => unwrap(api.GET("/api/v1/research-configs")));
  const connections = useTenantQuery(
    ["provider-connections"],
    () => unwrap(api.GET("/api/v1/provider-connections")),
    { enabled: can("integrations.manage") },
  );
  const [form, setForm] = useState({ name: "", cities: "", categories: "", services: "", cost_cap: "1", qualified_target: "25" });
  const ids = { name: useId(), cities: useId(), categories: useId(), services: useId(), cost: useId(), target: useId() };
  const split = (value: string) => value.split(",").map((part) => part.trim()).filter(Boolean);
  const pick = (purpose: string) => connections.data?.find((c) => c.purpose === purpose && c.status !== "revoked")?.id ?? null;
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, ["research-configs"]) });
    void queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, ["research-runs"]) });
  };
  const create = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/research-configs", {
          body: {
            name: form.name,
            country: "Bulgaria",
            cities: split(form.cities),
            categories: split(form.categories),
            services: split(form.services),
            cost_cap: form.cost_cap,
            candidate_cap: 100,
            qualified_target: Number(form.qualified_target),
            discovery_connection_id: pick("discovery"),
            model_connection_id: pick("model"),
          },
        }),
      ),
    onSuccess: invalidate,
  });
  const start = useMutation({
    mutationFn: (configId: string) =>
      unwrap(api.POST("/api/v1/research-configs/{config_id}/runs", { params: { path: { config_id: configId } }, body: {} })),
    onSettled: invalidate,
  });
  const set = (key: keyof typeof form) => (event: { target: { value: string } }) => setForm((f) => ({ ...f, [key]: event.target.value }));

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    create.mutate();
  }

  return (
    <section className="panel" aria-labelledby="configs-heading">
      <h2 id="configs-heading">Research set-ups</h2>
      {configs.isPending ? (
        <Loading label="Loading set-ups" />
      ) : configs.isError ? (
        <ErrorState error={configs.error} onRetry={() => void configs.refetch()} />
      ) : configs.data.length === 0 ? (
        <EmptyState title="No research set-up yet." />
      ) : (
        <ul className="rows">
          {configs.data.map((config) => (
            <li key={config.id} className="row">
              <div className="row-main">
                <span className="row-title">{config.name}</span>
                <span className="muted">
                  {config.cities.join(", ")} · {config.categories.join(", ")} · up to {config.qualified_target} qualified · cost cap{" "}
                  {config.cost_cap} · {config.cadence}
                </span>
              </div>
              {canRun ? (
                <button type="button" className="button button-primary" disabled={start.isPending} onClick={() => start.mutate(config.id)}>
                  Start run<span className="visually-hidden"> for {config.name}</span>
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {start.isError ? <ErrorState error={start.error} /> : null}
      {canRun ? (
        <details>
          <summary>New set-up</summary>
          <form className="stack" onSubmit={onSubmit}>
            <div className="field">
              <label htmlFor={ids.name}>Name</label>
              <input id={ids.name} required maxLength={120} value={form.name} onChange={set("name")} />
            </div>
            <div className="grid-2">
              <div className="field">
                <label htmlFor={ids.cities}>Cities (comma-separated)</label>
                <input id={ids.cities} required value={form.cities} onChange={set("cities")} />
              </div>
              <div className="field">
                <label htmlFor={ids.categories}>Business types (comma-separated)</label>
                <input id={ids.categories} required value={form.categories} onChange={set("categories")} />
              </div>
            </div>
            <div className="field">
              <label htmlFor={ids.services}>Services you offer (comma-separated)</label>
              <input id={ids.services} value={form.services} onChange={set("services")} />
            </div>
            <div className="grid-2">
              <div className="field">
                <label htmlFor={ids.cost}>Cost cap for one run</label>
                <input id={ids.cost} required inputMode="decimal" pattern="[0-9]+([.][0-9]{1,4})?" value={form.cost_cap} onChange={set("cost_cap")} />
              </div>
              <div className="field">
                <label htmlFor={ids.target}>Qualified candidates wanted</label>
                <input id={ids.target} required inputMode="numeric" pattern="[0-9]+" value={form.qualified_target} onChange={set("qualified_target")} />
              </div>
            </div>
            <p className="muted">
              Uses the connected discovery and model providers from <Link to="/integrations">Integrations</Link>, and needs a
              research budget.
            </p>
            {create.isError ? <ErrorState error={create.error} /> : null}
            <button type="submit" className="button" disabled={create.isPending}>
              Save set-up
            </button>
          </form>
        </details>
      ) : null}
    </section>
  );
}

function Runs({ canRun }: { canRun: boolean }) {
  const { tenantId, me } = useAuth();
  const queryClient = useQueryClient();
  const runs = useTenantQuery(["research-runs"], () => unwrap(api.GET("/api/v1/research-runs", { params: { query: { limit: 10 } } })), {
    refetchInterval: (query) => (query.state.data?.items.some((run: Run) => ACTIVE.has(run.status)) ? 3000 : false),
  });
  const control = useMutation({
    mutationFn: ({ id, action }: { id: string; action: "pause" | "resume" | "cancel" }) =>
      unwrap(api.POST("/api/v1/research-runs/{run_id}/{action}", { params: { path: { run_id: id, action } } })),
    onSettled: () => queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, ["research-runs"]) }),
  });
  return (
    <section className="panel" aria-labelledby="runs-heading">
      <h2 id="runs-heading">Runs</h2>
      {control.isError ? <ErrorState error={control.error} /> : null}
      {runs.isPending ? (
        <Loading label="Loading runs" />
      ) : runs.isError ? (
        <ErrorState error={runs.error} onRetry={() => void runs.refetch()} />
      ) : runs.data.items.length === 0 ? (
        <EmptyState title="No runs yet." />
      ) : (
        <ul className="rows">
          {runs.data.items.map((run: Run) => {
            const summary = run.summary as { shortfall_note?: string | null; stop_reason?: string; qualified?: number; coverage_gaps?: unknown[] };
            return (
              <li key={run.id} className="row">
                <div className="row-main">
                  <span className="row-title">
                    {run.status} · {run.searches_done} of {run.searches_total} searches
                  </span>
                  <progress max={Math.max(run.searches_total, 1)} value={run.searches_done} aria-label="Search progress" />
                  <span className="muted">
                    Started {formatDateTime(run.started_at ?? run.created_at, me.active_tenant?.timezone)} · estimated cost{" "}
                    {formatMoney(run.estimated_cost, me.active_tenant?.currency)} (not a bill)
                    {run.error ? ` · ${run.error}` : ""}
                  </span>
                  {summary.shortfall_note ? <span className="badge badge-warn">{summary.shortfall_note}</span> : null}
                  {summary.coverage_gaps?.length ? (
                    <span className="muted">{summary.coverage_gaps.length} searches were not completed; see the run summary.</span>
                  ) : null}
                </div>
                {canRun ? (
                  <div className="row-actions">
                    {run.status === "running" || run.status === "queued" ? (
                      <button type="button" className="button" onClick={() => control.mutate({ id: run.id, action: "pause" })}>
                        Pause
                      </button>
                    ) : null}
                    {run.status === "paused" ? (
                      <button type="button" className="button" onClick={() => control.mutate({ id: run.id, action: "resume" })}>
                        Resume
                      </button>
                    ) : null}
                    {["running", "queued", "paused"].includes(run.status) ? (
                      <button type="button" className="button" onClick={() => control.mutate({ id: run.id, action: "cancel" })}>
                        Cancel
                      </button>
                    ) : null}
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function ReviewQueue({ canReview }: { canReview: boolean }) {
  const { tenantId } = useAuth();
  const queryClient = useQueryClient();
  const [state, setState] = useState<"qualified" | "needs_review">("qualified");
  const key = ["research-candidates", state];
  const candidates = useTenantQuery(key, () =>
    unwrap(api.GET("/api/v1/research-candidates", { params: { query: { state, limit: 50 } } })),
  );
  const decide = useMutation({
    mutationFn: ({ id, accept }: { id: string; accept: boolean }) =>
      accept
        ? unwrap(api.POST("/api/v1/research-candidates/{candidate_id}/promote", { params: { path: { candidate_id: id } }, body: {} }))
        : unwrap(
            api.POST("/api/v1/research-candidates/{candidate_id}/reject", {
              params: { path: { candidate_id: id } },
              body: { reason: "Rejected in review" },
            }),
          ),
    onSettled: () => queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, ["research-candidates"]) }),
  });
  const stateId = useId();
  return (
    <section className="panel" aria-labelledby="review-heading">
      <h2 id="review-heading">Candidates to review</h2>
      <div className="field">
        <label htmlFor={stateId}>Show</label>
        <select id={stateId} value={state} onChange={(event) => setState(event.target.value as typeof state)}>
          <option value="qualified">Qualified by the checks</option>
          <option value="needs_review">Needs a closer look</option>
        </select>
      </div>
      {decide.isError ? <ErrorState error={decide.error} /> : null}
      {candidates.isPending ? (
        <Loading label="Loading candidates" />
      ) : candidates.isError ? (
        <ErrorState error={candidates.error} onRetry={() => void candidates.refetch()} />
      ) : candidates.data.items.length === 0 ? (
        <EmptyState title="Nothing to review." />
      ) : (
        <ul className="prospect-cards">
          {candidates.data.items.map((candidate: Candidate) => (
            <CandidateCard key={candidate.id} candidate={candidate} canReview={canReview} busy={decide.isPending} decide={decide.mutate} />
          ))}
        </ul>
      )}
    </section>
  );
}

function CandidateCard({
  candidate,
  canReview,
  busy,
  decide,
}: {
  candidate: Candidate;
  canReview: boolean;
  busy: boolean;
  decide: (input: { id: string; accept: boolean }) => void;
}) {
  const proposal = candidate.proposal as {
    observations?: { text: string; evidence_url: string }[];
    hypotheses?: string[];
    score?: { total: number; tier: string } | null;
    confidence?: string;
    opening?: string | null;
  };
  const acceptable = Boolean(candidate.name) && Boolean(proposal.observations?.length);
  return (
    <li className="prospect-card">
      <div className="prospect-head">
        <span className="row-title">{candidate.name ?? "Name not stored (needs confirming by hand)"}</span>
        {proposal.score ? (
          <span className={`badge tier tier-${proposal.score.tier}`}>
            Proposed: Tier {proposal.score.tier} · {proposal.score.total}
          </span>
        ) : (
          <span className="badge">No score proposed</span>
        )}
      </div>
      <span className="muted">
        {[candidate.category, candidate.city].filter(Boolean).join(" · ")}
        {candidate.website_url ? (
          <>
            {" · "}
            <a href={candidate.website_url} target="_blank" rel="noopener noreferrer">
              website
            </a>
          </>
        ) : null}
      </span>
      {candidate.state_reason ? <p className="notice notice-warn">{candidate.state_reason}</p> : null}
      {proposal.observations?.map((observation) => (
        <p key={observation.text} className="finding">
          <strong>Observed:</strong> {observation.text}{" "}
          <a href={observation.evidence_url} target="_blank" rel="noopener noreferrer">
            source
          </a>
        </p>
      ))}
      {proposal.hypotheses?.map((hypothesis) => (
        <p key={hypothesis} className="muted">
          Assumed, to confirm: {hypothesis}
        </p>
      ))}
      <div className="badges">
        {proposal.confidence ? <span className="badge">Confidence: {proposal.confidence}</span> : null}
        {candidate.website_match === "ambiguous" ? <span className="badge badge-warn">Website match uncertain</span> : null}
        {candidate.dropped_fields.length ? (
          <span className="badge">Not stored from the listing: {candidate.dropped_fields.join(", ").replace(/_/g, " ")}</span>
        ) : null}
      </div>
      {canReview ? (
        <div className="row-actions">
          <button type="button" className="button button-primary" disabled={busy || !acceptable} onClick={() => decide({ id: candidate.id, accept: true })}>
            Add as lead<span className="visually-hidden">: {candidate.name}</span>
          </button>
          <button type="button" className="button" disabled={busy} onClick={() => decide({ id: candidate.id, accept: false })}>
            Reject<span className="visually-hidden">: {candidate.name}</span>
          </button>
          {!acceptable ? <span className="muted">Needs a name and evidence from its own website before it can be added here.</span> : null}
        </div>
      ) : null}
    </li>
  );
}
