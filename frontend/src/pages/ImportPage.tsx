import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";
import { Link } from "react-router-dom";

import { api, ApiError, type Schemas, unwrap } from "../api/client";
import { tenantKey, useAuth, useTenantQuery } from "../auth/AuthContext";
import { EmptyState, ErrorState, Loading } from "../components/States";

type Import = Schemas["ImportOut"];
type Counts = Record<string, number>;

interface Report {
  sheet?: string;
  mapping?: Record<string, string>;
  unmapped_headers?: string[];
  counts?: Counts;
  tiers?: Counts;
  confidence?: Counts;
  missing_not_found?: Counts;
  score_mismatches?: number;
  formula_cells_without_cache?: number;
  shortlist?: { sheet: string; rows: number; unresolved_lead_ids: string[] } | null;
}

const IN_PROGRESS = new Set(["uploaded", "parsing", "committing"]);

function readCsrf(): string {
  const entry = document.cookie.split("; ").find((part) => part.startsWith("crm_csrf="));
  return entry ? decodeURIComponent(entry.slice("crm_csrf=".length)) : "";
}

/** Multipart upload. The generated client is JSON-only, so this one request is made by hand. */
async function uploadFile(file: File): Promise<Import> {
  const body = new FormData();
  body.append("file", file);
  const response = await fetch(`${window.location.origin}/api/v1/imports`, {
    method: "POST",
    body,
    credentials: "same-origin",
    headers: { "X-CSRF-Token": readCsrf() },
  });
  const payload = await response.json().catch(() => undefined);
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload as Import;
}

export function ImportPage() {
  const { tenantId, can } = useAuth();
  const queryClient = useQueryClient();
  const [importId, setImportId] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const fileId = useId();

  const upload = useMutation({ mutationFn: uploadFile, onSuccess: (created) => setImportId(created.id) });
  const current = useQuery({
    queryKey: tenantKey(tenantId, ["import", importId]),
    queryFn: () =>
      unwrap(api.GET("/api/v1/imports/{import_id}", { params: { path: { import_id: importId ?? "" } } })),
    enabled: importId !== null,
    refetchInterval: (query) => (query.state.data && IN_PROGRESS.has(query.state.data.status) ? 1500 : false),
  });
  const commit = useMutation({
    mutationFn: () =>
      unwrap(api.POST("/api/v1/imports/{import_id}/commit", { params: { path: { import_id: importId ?? "" } } })),
    onSettled: async () => {
      await current.refetch();
      await queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, ["companies"]) });
    },
  });

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (file) upload.mutate(file);
  }

  if (!can("import.run")) {
    return <EmptyState title="You do not have permission to import prospect lists." />;
  }
  const data = current.data;

  return (
    <>
      <div className="page-header">
        <h1>Import prospects</h1>
        {can("export.run") ? (
          <a className="button" href="/api/v1/exports/prospects.xlsx" download>
            Export workbook
          </a>
        ) : null}
      </div>
      <form className="panel stack" onSubmit={onSubmit}>
        <div className="field">
          <label htmlFor={fileId}>Prospect list (.xlsx or .csv, up to 10 MB)</label>
          <input
            id={fileId}
            type="file"
            accept=".xlsx,.csv"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
        </div>
        <p className="muted">
          The file is checked first and you review the result. Nothing is added to the CRM until you confirm.
          Formulas and macros are never run.
        </p>
        {upload.isError ? <ErrorState error={upload.error} /> : null}
        <button type="submit" className="button button-primary" disabled={upload.isPending || file === null}>
          {upload.isPending ? "Uploading…" : "Check file"}
        </button>
      </form>

      {importId === null ? null : current.isPending ? (
        <Loading label="Checking file" />
      ) : current.isError ? (
        <ErrorState error={current.error} onRetry={() => void current.refetch()} />
      ) : data ? (
        <ImportReview data={data} commit={() => commit.mutate()} committing={commit.isPending} error={commit.error} />
      ) : null}
    </>
  );
}

function ImportReview({
  data,
  commit,
  committing,
  error,
}: {
  data: Import;
  commit: () => void;
  committing: boolean;
  error: unknown;
}) {
  const report = data.report as Report;
  const counts = report.counts ?? {};
  if (IN_PROGRESS.has(data.status)) {
    return <Loading label={data.status === "committing" ? "Importing" : "Checking file"} />;
  }
  if (data.status === "failed") {
    return (
      <div className="state state-error" role="alert">
        <p className="state-title">This file could not be imported.</p>
        <p>{data.error}</p>
      </div>
    );
  }
  if (data.status === "committed") {
    const result = data.result as Counts;
    return (
      <div className="notice" role="status">
        <p>
          <strong>Import complete.</strong> {result.created ?? 0} added, {result.updated ?? 0} updated,{" "}
          {result.unchanged ?? 0} unchanged, {result.skipped ?? 0} skipped.
        </p>
        <Link to="/prospects">Open prospects</Link>
      </div>
    );
  }
  const willImport = (counts.create ?? 0) + (counts.update ?? 0);
  return (
    <section className="panel" aria-labelledby="review-heading">
      <h2 id="review-heading">Review: {data.filename}</h2>
      <dl className="facts">
        <Fact label="Rows read" value={counts.rows ?? 0} />
        <Fact label="New leads" value={counts.create ?? 0} />
        <Fact label="Existing leads to update" value={counts.update ?? 0} />
        <Fact label="Rows skipped" value={counts.skip ?? 0} />
        <Fact label="Rows with errors" value={counts.with_errors ?? 0} />
        <Fact label="Rows with warnings" value={counts.with_warnings ?? 0} />
        <Fact label="Tiers (computed)" value={formatCounts(report.tiers)} />
        <Fact label="Confidence" value={formatCounts(report.confidence)} />
        <Fact label="Source totals that differ" value={report.score_mismatches ?? 0} />
        <Fact label="Marked “Not found”" value={formatCounts(report.missing_not_found)} />
      </dl>
      {report.shortlist ? (
        <p>
          Shortlist sheet “{report.shortlist.sheet}”: {report.shortlist.rows} rows linked to the leads above. It adds
          no leads of its own.
          {report.shortlist.unresolved_lead_ids.length
            ? ` ${report.shortlist.unresolved_lead_ids.length} refer to a Lead ID that is not in the list.`
            : ""}
        </p>
      ) : null}
      {report.formula_cells_without_cache ? (
        <p className="notice notice-warn">
          {report.formula_cells_without_cache} formula cells had no stored result and were read as empty. Scores are
          always recalculated from their components.
        </p>
      ) : null}
      <details>
        <summary>Column mapping ({Object.keys(report.mapping ?? {}).length} fields)</summary>
        <ul className="rows">
          {Object.entries(report.mapping ?? {}).map(([field, header]) => (
            <li key={field} className="row">
              <span>{header}</span>
              <span className="badge">{field.replace(/_/g, " ")}</span>
            </li>
          ))}
        </ul>
        {report.unmapped_headers?.length ? (
          <p className="muted">Kept but not mapped: {report.unmapped_headers.join(", ")}</p>
        ) : null}
      </details>
      {(counts.with_errors ?? 0) + (counts.with_warnings ?? 0) > 0 ? <RowIssues importId={data.id} /> : null}
      {error ? <ErrorState error={error} /> : null}
      <button type="button" className="button button-primary" disabled={committing || willImport === 0} onClick={commit}>
        {committing ? "Importing…" : `Import ${willImport} leads`}
      </button>
    </section>
  );
}

function RowIssues({ importId }: { importId: string }) {
  const rows = useTenantQuery(["import-rows", importId], () =>
    unwrap(
      api.GET("/api/v1/imports/{import_id}/rows", {
        params: { path: { import_id: importId }, query: { only: "issues", limit: 100 } },
      }),
    ),
  );
  if (rows.isPending) return <Loading label="Loading issues" />;
  if (rows.isError) return <ErrorState error={rows.error} onRetry={() => void rows.refetch()} />;
  return (
    <details open>
      <summary>Rows needing attention ({rows.data.total})</summary>
      <ul className="rows">
        {rows.data.items.map((row) => (
          <li key={row.id} className="row">
            <div className="row-main">
              <span className="row-title">
                Row {row.row_number}: {row.business_name ?? "No name"} {row.external_id ? `(${row.external_id})` : ""}
              </span>
              {row.issues
                .filter((issue) => issue.severity !== "info")
                .map((issue, index) => (
                  <span key={index} className="muted">
                    {issue.severity === "error" ? "Error" : "Warning"}: {issue.message}
                  </span>
                ))}
            </div>
            <span className="badge">{row.action === "skip" ? "Will be skipped" : `Will ${row.action}`}</span>
          </li>
        ))}
      </ul>
    </details>
  );
}

function Fact({ label, value }: { label: string; value: string | number }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function formatCounts(counts: Counts | undefined): string {
  const entries = Object.entries(counts ?? {});
  return entries.length ? entries.map(([key, value]) => `${key.replace(/_/g, " ")}: ${value}`).join(", ") : "—";
}
