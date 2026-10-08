import { useId, useState } from "react";
import { Link } from "react-router-dom";

import { api, unwrap } from "../api/client";
import { useAuth, useTenantQuery } from "../auth/AuthContext";
import { Pagination } from "../components/Pagination";
import { actionReason, ProspectCard, TierBadge, VerificationBadges } from "../components/ProspectBits";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { formatDate } from "../lib/format";

const PAGE_SIZE = 50;
type Filters = {
  q: string;
  city: string;
  industry_group: string;
  service: string;
  tag: string;
  tier: string;
  confidence: string;
  freshness: string;
  channel: string;
  outreach_status: string;
  needs_verification: boolean;
  callable_only: boolean;
};
const EMPTY: Filters = {
  q: "",
  city: "",
  industry_group: "",
  service: "",
  tag: "",
  tier: "",
  confidence: "",
  freshness: "",
  channel: "",
  outreach_status: "",
  needs_verification: false,
  callable_only: false,
};

export function ProspectsPage({ verificationOnly = false }: { verificationOnly?: boolean }) {
  const { me } = useAuth();
  const [filters, setFilters] = useState<Filters>({ ...EMPTY, needs_verification: verificationOnly });
  const [offset, setOffset] = useState(0);
  const idBase = useId();
  const facets = useTenantQuery(["prospect-facets"], () => unwrap(api.GET("/api/v1/prospects/facets")));

  const query = {
    ...Object.fromEntries(Object.entries(filters).filter(([, value]) => value !== "" && value !== false)),
    limit: PAGE_SIZE,
    offset,
  };
  const prospects = useTenantQuery(["prospects", query], () =>
    unwrap(api.GET("/api/v1/prospects", { params: { query: query as never } })),
  );
  const set = <K extends keyof Filters>(key: K, value: Filters[K]) => {
    setFilters((current) => ({ ...current, [key]: value }));
    setOffset(0);
  };
  const select = (key: keyof Filters, label: string, options: (string | [string, string])[]) => (
    <div className="field">
      <label htmlFor={`${idBase}-${key}`}>{label}</label>
      <select
        id={`${idBase}-${key}`}
        value={String(filters[key])}
        onChange={(event) => set(key, event.target.value as never)}
      >
        <option value="">Any</option>
        {options.map((option) => {
          const [value, text] = Array.isArray(option) ? option : [option, option];
          return (
            <option key={value} value={value}>
              {text}
            </option>
          );
        })}
      </select>
    </div>
  );

  return (
    <>
      <h1>{verificationOnly ? "Needs verification" : "Prospects"}</h1>
      <p className="muted">
        Ranked by score, then confidence, then most recently checked, then Lead ID. A score orders the list; it is
        not a probability of a sale.
      </p>
      <form className="filters" role="search" onSubmit={(event) => event.preventDefault()}>
        <div className="field">
          <label htmlFor={`${idBase}-q`}>Search</label>
          <input id={`${idBase}-q`} type="search" value={filters.q} onChange={(e) => set("q", e.target.value)} />
        </div>
        {select("tier", "Tier", ["A", "B", "C", ["unscored", "Unscored"]])}
        {select("confidence", "Confidence", ["high", "medium", "low"])}
        {select("city", "City", facets.data?.cities ?? [])}
        {select("industry_group", "Industry", facets.data?.industry_groups ?? [])}
        {select("service", "Service", facets.data?.services ?? [])}
        {select("tag", "Issue", facets.data?.tags ?? [])}
        {select("channel", "Preferred channel", ["phone", "email"])}
        {select("freshness", "Evidence", [
          ["fresh", "Recent"],
          ["stale", "Old"],
        ])}
        {select("outreach_status", "Status", facets.data?.outreach_statuses ?? [])}
        <label className="check">
          <input
            type="checkbox"
            checked={filters.needs_verification}
            onChange={(e) => set("needs_verification", e.target.checked)}
          />
          Needs verification
        </label>
        <label className="check">
          <input type="checkbox" checked={filters.callable_only} onChange={(e) => set("callable_only", e.target.checked)} />
          Can be called
        </label>
      </form>

      {prospects.isPending ? (
        <Loading label="Loading prospects" />
      ) : prospects.isError ? (
        <ErrorState error={prospects.error} onRetry={() => void prospects.refetch()} />
      ) : prospects.data.items.length === 0 ? (
        <EmptyState title="No prospects match.">
          <p>
            Change the filters, or <Link to="/import">import a prospect list</Link>.
          </p>
        </EmptyState>
      ) : (
        <>
          <p aria-live="polite">{prospects.data.total} prospects</p>
          <div className="table-wrap desktop-only">
            <table>
              <thead>
                <tr>
                  <th scope="col">Business</th>
                  <th scope="col">Score</th>
                  <th scope="col">Confidence</th>
                  <th scope="col">Strongest finding</th>
                  <th scope="col">Service</th>
                  <th scope="col">Contact</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {prospects.data.items.map((p) => (
                  <tr key={p.lead_id}>
                    <th scope="row">
                      <Link to={`/prospects/${p.lead_id}`}>{p.company_name}</Link>
                      <div className="muted">{[p.city, p.business_type].filter(Boolean).join(" · ")}</div>
                    </th>
                    <td>
                      <TierBadge prospect={p} />
                    </td>
                    <td>
                      {p.confidence}
                      <div className="muted">Checked {formatDate(p.checked_on)}</div>
                      <VerificationBadges prospect={p} />
                    </td>
                    <td className="cell-wide">
                      {p.finding ?? <span className="muted">No finding recorded</span>}
                      {p.evidence_url ? (
                        <div>
                          <a href={p.evidence_url} target="_blank" rel="noopener noreferrer">
                            Source
                          </a>
                        </div>
                      ) : null}
                    </td>
                    <td>{p.service_category ?? "—"}</td>
                    <td>
                      {p.actions.call?.available ? `${p.dialable_count} phone` : actionReason(p.actions.call?.reason)}
                      <div className="muted">{p.email_count ? `${p.email_count} email` : "No email found"}</div>
                    </td>
                    <td>{p.outreach_status.replace(/_/g, " ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <ul className="prospect-cards mobile-only">
            {prospects.data.items.map((p) => (
              <ProspectCard key={p.lead_id} prospect={p} timeZone={me.active_tenant?.timezone} />
            ))}
          </ul>
          <Pagination total={prospects.data.total} limit={PAGE_SIZE} offset={offset} onChange={setOffset} />
        </>
      )}
    </>
  );
}
