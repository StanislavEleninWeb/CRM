import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useId, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api, unwrap } from "../api/client";
import { tenantKey, useAuth, useTenantQuery } from "../auth/AuthContext";
import { Pagination } from "../components/Pagination";
import { EmptyState, ErrorState, Loading } from "../components/States";

const PAGE_SIZE = 50;

function useDebounced<T>(value: T, delay = 250): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

export function CompaniesPage() {
  const { can } = useAuth();
  const [search, setSearch] = useState("");
  const [city, setCity] = useState("");
  const [archived, setArchived] = useState(false);
  const [sort, setSort] = useState("name");
  const [offset, setOffset] = useState(0);
  const [creating, setCreating] = useState(false);
  const q = useDebounced(search.trim());
  const cityFilter = useDebounced(city.trim());
  const searchId = useId();
  const cityId = useId();
  const sortId = useId();

  const query = { q: q || undefined, city: cityFilter || undefined, archived, sort, limit: PAGE_SIZE, offset };
  const companies = useTenantQuery(["companies", query], () =>
    unwrap(api.GET("/api/v1/companies", { params: { query } })),
  );
  const resetPage = () => setOffset(0);

  return (
    <>
      <div className="page-header">
        <h1>Companies</h1>
        {can("crm.write") ? (
          <button type="button" className="button button-primary" onClick={() => setCreating((v) => !v)}>
            {creating ? "Cancel" : "New company"}
          </button>
        ) : null}
      </div>
      {creating ? <NewCompanyForm onDone={() => setCreating(false)} /> : null}

      <form className="inline-form" role="search" onSubmit={(event) => event.preventDefault()}>
        <div className="field">
          <label htmlFor={searchId}>Search</label>
          <input
            id={searchId}
            type="search"
            value={search}
            placeholder="Name, domain or ID"
            onChange={(event) => {
              setSearch(event.target.value);
              resetPage();
            }}
          />
        </div>
        <div className="field">
          <label htmlFor={cityId}>City</label>
          <input
            id={cityId}
            value={city}
            onChange={(event) => {
              setCity(event.target.value);
              resetPage();
            }}
          />
        </div>
        <div className="field">
          <label htmlFor={sortId}>Sort</label>
          <select id={sortId} value={sort} onChange={(event) => setSort(event.target.value)}>
            <option value="name">Name</option>
            <option value="-created_at">Newest</option>
            <option value="next_action_at">Next action</option>
            <option value="city">City</option>
          </select>
        </div>
        <label className="check">
          <input
            type="checkbox"
            checked={archived}
            onChange={(event) => {
              setArchived(event.target.checked);
              resetPage();
            }}
          />
          Archived
        </label>
      </form>

      {companies.isPending ? (
        <Loading label="Loading companies" />
      ) : companies.isError ? (
        <ErrorState error={companies.error} onRetry={() => void companies.refetch()} />
      ) : companies.data.items.length === 0 ? (
        <EmptyState title={q || cityFilter ? "No companies match these filters." : "No companies yet."}>
          {!q && !cityFilter ? <p>Add a company or import a prospect list to get started.</p> : null}
        </EmptyState>
      ) : (
        <>
          <ul className="rows panel">
            {companies.data.items.map((company) => (
              <li key={company.id} className="row">
                <div className="row-main">
                  <Link className="row-title" to={`/companies/${company.id}`}>
                    {company.name}
                  </Link>
                  <span className="muted">
                    {[company.business_type, company.city, company.domain].filter(Boolean).join(" · ") ||
                      "No details yet"}
                  </span>
                </div>
                {company.next_action ? <span className="badge">{company.next_action}</span> : null}
              </li>
            ))}
          </ul>
          <Pagination
            total={companies.data.total}
            limit={PAGE_SIZE}
            offset={offset}
            onChange={setOffset}
          />
        </>
      )}
    </>
  );
}

function NewCompanyForm({ onDone }: { onDone: () => void }) {
  const { tenantId } = useAuth();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [form, setForm] = useState({ name: "", city: "", country: "Bulgaria", website_url: "" });
  const ids = { name: useId(), city: useId(), country: useId(), website: useId() };
  const create = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/companies", {
          body: {
            name: form.name,
            city: form.city || null,
            country: form.country || null,
            website_url: form.website_url || null,
          },
        }),
      ),
    onSuccess: async (company) => {
      await queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, ["companies"]) });
      onDone();
      navigate(`/companies/${company.id}`);
    },
  });
  const set = (key: keyof typeof form) => (event: { target: { value: string } }) =>
    setForm((current) => ({ ...current, [key]: event.target.value }));

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    create.mutate();
  }

  return (
    <form className="panel stack" onSubmit={onSubmit} aria-label="New company">
      <div className="field">
        <label htmlFor={ids.name}>Name</label>
        <input id={ids.name} required maxLength={300} value={form.name} onChange={set("name")} />
      </div>
      <div className="grid-2">
        <div className="field">
          <label htmlFor={ids.city}>City</label>
          <input id={ids.city} value={form.city} onChange={set("city")} />
        </div>
        <div className="field">
          <label htmlFor={ids.country}>Country</label>
          <input id={ids.country} value={form.country} onChange={set("country")} />
        </div>
      </div>
      <div className="field">
        <label htmlFor={ids.website}>Website</label>
        <input id={ids.website} inputMode="url" value={form.website_url} onChange={set("website_url")} />
      </div>
      {create.isError ? <ErrorState error={create.error} /> : null}
      <button type="submit" className="button button-primary" disabled={create.isPending}>
        {create.isPending ? "Saving…" : "Create company"}
      </button>
    </form>
  );
}
