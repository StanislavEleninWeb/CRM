import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api, type Schemas, unwrap } from "../api/client";
import { tenantKey, useAuth, useTenantQuery } from "../auth/AuthContext";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { formatMoney } from "../lib/format";

type Deal = Schemas["DealOut"];
type Stage = Schemas["StageOut"];

/**
 * A board grouped by stage. Moving an opportunity is done with a select on each card,
 * which works with a keyboard, a screen reader and a touch screen alike.
 */
export function DealsPage() {
  const { tenantId, can } = useAuth();
  const queryClient = useQueryClient();
  const pipelines = useTenantQuery(["pipelines"], () => unwrap(api.GET("/api/v1/pipelines")));
  const pipeline = pipelines.data?.[0];
  const deals = useTenantQuery(
    ["deals", { pipeline_id: pipeline?.id }],
    () => unwrap(api.GET("/api/v1/deals", { params: { query: { pipeline_id: pipeline?.id, limit: 200 } } })),
    { enabled: Boolean(pipeline) },
  );
  const [pendingLoss, setPendingLoss] = useState<{ deal: Deal; stage: Stage } | null>(null);
  const [lossReason, setLossReason] = useState("");
  const move = useMutation({
    mutationFn: ({ dealId, stageId, reason }: { dealId: string; stageId: string; reason?: string }) =>
      unwrap(
        api.PATCH("/api/v1/deals/{deal_id}", {
          params: { path: { deal_id: dealId } },
          body: { stage_id: stageId, ...(reason ? { loss_reason: reason } : {}) },
        }),
      ),
    onSuccess: () => {
      setPendingLoss(null);
      setLossReason("");
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, ["deals"]) }),
  });

  if (pipelines.isPending || (pipeline && deals.isPending)) return <Loading label="Loading opportunities" />;
  if (pipelines.isError) return <ErrorState error={pipelines.error} onRetry={() => void pipelines.refetch()} />;
  if (deals.isError) return <ErrorState error={deals.error} onRetry={() => void deals.refetch()} />;
  if (!pipeline) return <EmptyState title="No pipeline is configured." />;
  const items = deals.data?.items ?? [];

  function onStageChange(deal: Deal, stageId: string) {
    const stage = pipeline?.stages.find((candidate) => candidate.id === stageId);
    if (!stage) return;
    if (stage.kind === "lost") {
      setPendingLoss({ deal, stage });
      return;
    }
    move.mutate({ dealId: deal.id, stageId });
  }

  return (
    <>
      <h1>Opportunities</h1>
      <p className="muted">{pipeline.name} pipeline</p>
      {move.isError ? <ErrorState error={move.error} /> : null}
      {pendingLoss ? (
        <form
          className="panel stack"
          aria-label="Reason for loss"
          onSubmit={(event) => {
            event.preventDefault();
            move.mutate({ dealId: pendingLoss.deal.id, stageId: pendingLoss.stage.id, reason: lossReason });
          }}
        >
          <div className="field">
            <label htmlFor="loss-reason">Why was “{pendingLoss.deal.title}” lost?</label>
            <input id="loss-reason" required maxLength={500} value={lossReason} onChange={(e) => setLossReason(e.target.value)} />
          </div>
          <div className="row-actions">
            <button type="submit" className="button button-primary" disabled={move.isPending}>
              Mark as lost
            </button>
            <button type="button" className="button" onClick={() => setPendingLoss(null)}>
              Cancel
            </button>
          </div>
        </form>
      ) : null}
      {items.length === 0 ? (
        <EmptyState title="No opportunities yet.">
          <p>Convert a qualified lead to create one.</p>
        </EmptyState>
      ) : (
        <div className="board">
          {pipeline.stages.map((stage) => {
            const inStage = items.filter((deal) => deal.stage_id === stage.id);
            return (
              <section key={stage.id} className="board-column" aria-labelledby={`stage-${stage.id}`}>
                <h2 id={`stage-${stage.id}`}>
                  {stage.name} <span className="muted">({inStage.length})</span>
                </h2>
                <ul className="board-cards">
                  {inStage.map((deal) => (
                    <li key={deal.id} className="board-card">
                      <span className="row-title">{deal.title}</span>
                      <Link to={`/companies/${deal.company_id}`}>{deal.company_name}</Link>
                      <span className="muted">{formatMoney(deal.amount, deal.currency)}</span>
                      {deal.next_action ? <span className="muted">Next: {deal.next_action}</span> : null}
                      {can("crm.write") ? (
                        <>
                          <label className="visually-hidden" htmlFor={`move-${deal.id}`}>
                            Stage for {deal.title}
                          </label>
                          <select
                            id={`move-${deal.id}`}
                            value={deal.stage_id}
                            disabled={move.isPending}
                            onChange={(event) => onStageChange(deal, event.target.value)}
                          >
                            {pipeline.stages.map((option) => (
                              <option key={option.id} value={option.id}>
                                {option.name}
                              </option>
                            ))}
                          </select>
                        </>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </section>
            );
          })}
        </div>
      )}
    </>
  );
}
