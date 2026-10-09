import { useState } from "react";
import { Link } from "react-router-dom";

import { api, type Schemas, unwrap } from "../api/client";
import { useTenantQuery } from "../auth/AuthContext";
import { ErrorState, Loading } from "../components/States";
import { formatDateTime } from "../lib/format";

type Metric = Schemas["Metric"];

const BASIS: Record<string, string> = {
  reported_by_people: "Reported by people",
  recorded_by_system: "Recorded by the application",
  from_mailbox: "From the mailbox",
  estimated_and_verified_costs: "Estimated and verified costs",
};

function shown(metric: Metric): string {
  if (metric.value === null || metric.value === undefined) return "No data";
  if (metric.unit === "percent") return `${metric.value}%`;
  if (metric.unit === "hours") return `${metric.value} h`;
  return String(metric.value);
}

/** What needs someone today. Used on the Today page. */
export function Attention() {
  const attention = useTenantQuery(["attention"], () => unwrap(api.GET("/api/v1/workspace/attention")));
  if (!attention.data || attention.data.items.length === 0) return null;
  return (
    <section className="panel" aria-labelledby="attention-heading">
      <h2 id="attention-heading">Needs attention</h2>
      <ul className="rows">
        {attention.data.items.map((item) => (
          <li key={item.key} className="row">
            <div className="row-main">
              <span className="row-title">
                <Link to={item.link}>
                  {item.count} · {item.label}
                </Link>
              </span>
              <span className="muted">{item.examples.join(" · ")}</span>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function ReportsPage() {
  const [range, setRange] = useState({ from: "", to: "" });
  const [open, setOpen] = useState<string | null>(null);
  const query = { from: range.from || undefined, to: range.to || undefined };
  const report = useTenantQuery(["funnel", range], () => unwrap(api.GET("/api/v1/reports/funnel", { params: { query } })));
  const records = useTenantQuery(
    ["report-records", open, range],
    () => unwrap(api.GET("/api/v1/reports/records", { params: { query: { metric: open ?? "", ...query } } })),
    { enabled: open !== null },
  );

  return (
    <>
      <h1>Reports</h1>
      <form className="inline-form" aria-label="Period" onSubmit={(event) => event.preventDefault()}>
        <div className="field">
          <label htmlFor="report-from">From</label>
          <input id="report-from" type="date" value={range.from} onChange={(e) => setRange({ ...range, from: e.target.value })} />
        </div>
        <div className="field">
          <label htmlFor="report-to">To</label>
          <input id="report-to" type="date" value={range.to} onChange={(e) => setRange({ ...range, to: e.target.value })} />
        </div>
      </form>
      {report.isPending ? (
        <Loading label="Loading the report" />
      ) : report.isError ? (
        <ErrorState error={report.error} onRetry={() => void report.refetch()} />
      ) : (
        <>
          <p className="muted">{report.data.cohort_rule}</p>
          <section className="panel" aria-labelledby="figures-heading">
            <h2 id="figures-heading">Figures</h2>
            <ul className="rows">
              {report.data.metrics.map((metric) => (
                <li key={metric.key} className="row">
                  <div className="row-main">
                    <span className="row-title">
                      {metric.label}: {shown(metric)}
                      {metric.numerator !== null && metric.numerator !== undefined && metric.denominator
                        ? ` (${metric.numerator} of ${metric.denominator})`
                        : ""}
                    </span>
                    <span className="muted">{metric.definition}</span>
                    <span className="muted">
                      {BASIS[metric.basis] ?? metric.basis}
                      {metric.note ? ` · ${metric.note}` : ""}
                    </span>
                  </div>
                  {metric.drill ? (
                    <div className="row-actions">
                      <button type="button" className="button" onClick={() => setOpen(open === metric.drill ? null : (metric.drill ?? null))}>
                        {open === metric.drill ? "Hide records" : "Show records"}
                        <span className="visually-hidden"> for {metric.label}</span>
                      </button>
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
            {open && records.isError ? <ErrorState error={records.error} /> : null}
            {open && records.data ? (
              <ul className="rows" aria-label="Records behind the figure">
                {records.data.length === 0 ? <li className="muted">No records in this period.</li> : null}
                {records.data.map((record) => (
                  <li key={record.id} className="row">
                    <div className="row-main">
                      <span>{record.link ? <Link to={record.link}>{record.label}</Link> : record.label}</span>
                      <span className="muted">{formatDateTime(record.at, report.data.timezone)}</span>
                    </div>
                  </li>
                ))}
              </ul>
            ) : null}
          </section>
          <section className="panel" aria-labelledby="stages-heading">
            <h2 id="stages-heading">Opportunity stages</h2>
            <ul className="rows">
              {report.data.stages.map((stage) => (
                <li key={stage.stage} className="row">
                  <div className="row-main">
                    <span className="row-title">{stage.stage}</span>
                    <span className="muted">
                      {stage.entered_in_period} entered in the period
                      {stage.kind === "open"
                        ? ` · ${stage.open_now} open now${
                            stage.average_days_in_stage !== null && stage.average_days_in_stage !== undefined
                              ? `, ${stage.average_days_in_stage} days in stage on average, oldest ${stage.oldest_days_in_stage}`
                              : ""
                          }`
                        : ""}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
            <h3>Won amounts</h3>
            {report.data.won_amounts.length === 0 ? <p className="muted">No data.</p> : null}
            {report.data.won_amounts.map((line) => (
              <p key={line.currency}>
                {line.amount} {line.currency}
              </p>
            ))}
            {report.data.won_amounts.length > 1 ? <p className="muted">Currencies are not added together.</p> : null}
            <h3>Research costs</h3>
            {report.data.research_costs.length === 0 ? <p className="muted">No data.</p> : null}
            {report.data.research_costs.map((line) => (
              <p key={`${line.currency}-${line.basis}`}>
                {line.amount} {line.currency} — {line.basis.replace(/_/g, " ")}
              </p>
            ))}
          </section>
          <section className="panel" aria-labelledby="missing-heading">
            <h2 id="missing-heading">Not measured</h2>
            <ul>
              {report.data.not_collected.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </section>
        </>
      )}
    </>
  );
}
