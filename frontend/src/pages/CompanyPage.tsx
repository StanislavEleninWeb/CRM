import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api, type Schemas, unwrap } from "../api/client";
import { tenantKey, useAuth, useTenantQuery } from "../auth/AuthContext";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { formatDateTime, formatMoney } from "../lib/format";

type Channel = Schemas["ChannelOut"];
const KIND_LABELS: Record<Channel["kind"], string> = {
  phone: "Phone",
  email: "Email",
  website: "Website",
  contact_page: "Contact page",
  social: "Social",
  messaging: "Messaging",
  other: "Other",
};

export function CompanyPage() {
  const { companyId = "" } = useParams();
  const { tenantId, can, me } = useAuth();
  const queryClient = useQueryClient();
  const key = ["company", companyId];
  const company = useTenantQuery(key, () =>
    unwrap(api.GET("/api/v1/companies/{company_id}", { params: { path: { company_id: companyId } } })),
  );
  const refresh = () => queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, key) });
  const timeZone = me.active_tenant?.timezone;

  if (company.isPending) return <Loading label="Loading company" />;
  if (company.isError) return <ErrorState error={company.error} onRetry={() => void company.refetch()} />;
  const data = company.data;
  const writable = can("crm.write");

  return (
    <>
      <p>
        <Link to="/companies">← Companies</Link>
      </p>
      <div className="page-header">
        <h1>{data.name}</h1>
        {data.archived_at ? <span className="badge">Archived</span> : null}
      </div>
      <p className="muted">
        {[data.business_type, data.city, data.country].filter(Boolean).join(" · ")}
        {data.website_url ? (
          <>
            {" · "}
            <a href={data.website_url} target="_blank" rel="noopener noreferrer">
              {data.domain}
            </a>
          </>
        ) : null}
      </p>
      {data.merged_into_id ? (
        <div className="notice notice-warn">
          This record was merged. <Link to={`/companies/${data.merged_into_id}`}>Open the current record</Link>.
        </div>
      ) : null}
      {data.tags.length ? (
        <p>
          {data.tags.map((tag) => (
            <span key={tag} className="badge">
              {tag}
            </span>
          ))}
        </p>
      ) : null}

      <section className="panel" aria-labelledby="next-heading">
        <h2 id="next-heading">Next action</h2>
        <p>
          {data.next_action ? (
            <>
              <strong>{data.next_action}</strong> · {formatDateTime(data.next_action_at, timeZone)}
            </>
          ) : (
            <span className="muted">Nothing planned.</span>
          )}
        </p>
      </section>

      <section className="panel" aria-labelledby="channels-heading">
        <h2 id="channels-heading">Contact channels</h2>
        {data.restrictions
          .filter((restriction) => !restriction.lifted_at)
          .map((restriction) => (
            <div key={restriction.id} className="notice notice-warn" role="note">
              Do not contact by {restriction.channel_kind === "any" ? "any channel" : restriction.channel_kind}:{" "}
              {restriction.reason}
            </div>
          ))}
        {data.channels.length === 0 ? (
          <EmptyState title="No contact details recorded." />
        ) : (
          <ul className="rows">
            {data.channels.map((channel) => (
              <ChannelRow key={channel.id} channel={channel} />
            ))}
          </ul>
        )}
        {writable ? <AddChannelForm companyId={companyId} onAdded={refresh} /> : null}
      </section>

      <section className="panel" aria-labelledby="people-heading">
        <h2 id="people-heading">People ({data.contacts.length})</h2>
        {data.contacts.length === 0 ? (
          <p className="muted">No named contact yet. A lead does not need one.</p>
        ) : (
          <ul className="rows">
            {data.contacts.map((contact) => (
              <li key={contact.id} className="row">
                <div className="row-main">
                  <span className="row-title">{contact.full_name}</span>
                  {contact.job_title ? <span className="muted">{contact.job_title}</span> : null}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <RelatedDeals companyId={companyId} />
      <Tasks companyId={companyId} writable={writable} timeZone={timeZone} />
      <Notes companyId={companyId} writable={writable} />
      <Timeline companyId={companyId} timeZone={timeZone} />
    </>
  );
}

function ChannelRow({ channel }: { channel: Channel }) {
  const value = channel.raw_value;
  let action = null;
  if (channel.kind === "phone") {
    action = channel.dial_uri ? (
      <a className="button" href={channel.dial_uri} aria-label={`Call ${value}`}>
        Call
      </a>
    ) : (
      <span className="badge badge-warn">
        {channel.purpose === "emergency" ? "Emergency line: not for sales calls" : "Do not call"}
      </span>
    );
  } else if (!channel.allow_sales_use) {
    action = <span className="badge badge-warn">Do not contact</span>;
  }
  return (
    <li className="row">
      <div className="row-main">
        <span className="row-title">{value}</span>
        <span className="muted">
          {KIND_LABELS[channel.kind]}
          {channel.purpose !== "general" ? ` · ${channel.purpose}` : ""}
          {channel.label ? ` · ${channel.label}` : ""}
          {` · ${channel.verification_state}`}
          {channel.restriction_reason ? ` · ${channel.restriction_reason}` : ""}
        </span>
      </div>
      {action}
    </li>
  );
}

function AddChannelForm({ companyId, onAdded }: { companyId: string; onAdded: () => void }) {
  const [kind, setKind] = useState<Channel["kind"]>("phone");
  const [purpose, setPurpose] = useState<Channel["purpose"]>("general");
  const [value, setValue] = useState("");
  const ids = { kind: useId(), purpose: useId(), value: useId() };
  const add = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/companies/{company_id}/channels", {
          params: { path: { company_id: companyId } },
          body: { kind, raw_value: value, purpose },
        }),
      ),
    onSuccess: () => {
      setValue("");
      onAdded();
    },
  });
  return (
    <form
      className="inline-form"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        add.mutate();
      }}
    >
      <div className="field">
        <label htmlFor={ids.kind}>Type</label>
        <select id={ids.kind} value={kind} onChange={(e) => setKind(e.target.value as Channel["kind"])}>
          {Object.entries(KIND_LABELS).map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </select>
      </div>
      {kind === "phone" ? (
        <div className="field">
          <label htmlFor={ids.purpose}>Purpose</label>
          <select
            id={ids.purpose}
            value={purpose}
            onChange={(e) => setPurpose(e.target.value as Channel["purpose"])}
          >
            <option value="general">General</option>
            <option value="booking">Booking</option>
            <option value="delivery">Delivery</option>
            <option value="emergency">Emergency</option>
            <option value="unknown">Unknown</option>
          </select>
        </div>
      ) : null}
      <div className="field">
        <label htmlFor={ids.value}>Value</label>
        <input id={ids.value} required value={value} onChange={(e) => setValue(e.target.value)} />
      </div>
      <button type="submit" className="button" disabled={add.isPending}>
        Add
      </button>
      {add.isError ? <ErrorState error={add.error} /> : null}
    </form>
  );
}

function RelatedDeals({ companyId }: { companyId: string }) {
  const deals = useTenantQuery(["deals", { company_id: companyId }], () =>
    unwrap(api.GET("/api/v1/deals", { params: { query: { company_id: companyId } } })),
  );
  return (
    <section className="panel" aria-labelledby="deals-heading">
      <h2 id="deals-heading">Opportunities</h2>
      {deals.isPending ? (
        <Loading label="Loading opportunities" />
      ) : deals.isError ? (
        <ErrorState error={deals.error} onRetry={() => void deals.refetch()} />
      ) : deals.data.items.length === 0 ? (
        <p className="muted">No opportunities yet.</p>
      ) : (
        <ul className="rows">
          {deals.data.items.map((deal) => (
            <li key={deal.id} className="row">
              <div className="row-main">
                <span className="row-title">{deal.title}</span>
                <span className="muted">
                  {deal.stage_name} · {formatMoney(deal.amount, deal.currency)}
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Tasks({ companyId, writable, timeZone }: { companyId: string; writable: boolean; timeZone?: string }) {
  const { tenantId } = useAuth();
  const queryClient = useQueryClient();
  const key = ["tasks", { company_id: companyId }];
  const tasks = useTenantQuery(key, () =>
    unwrap(api.GET("/api/v1/tasks", { params: { query: { company_id: companyId, status: "open" } } })),
  );
  const [title, setTitle] = useState("");
  const titleId = useId();
  const invalidate = () => queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, ["tasks"]) });
  const add = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/tasks", { body: { company_id: companyId, title } })),
    onSuccess: () => {
      setTitle("");
      void invalidate();
    },
  });
  const complete = useMutation({
    mutationFn: (taskId: string) =>
      unwrap(api.PATCH("/api/v1/tasks/{task_id}", { params: { path: { task_id: taskId } }, body: { status: "done" } })),
    onSettled: () => {
      void invalidate();
      void queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, ["activities"]) });
    },
  });
  return (
    <section className="panel" aria-labelledby="tasks-heading">
      <h2 id="tasks-heading">Open tasks</h2>
      {tasks.isPending ? (
        <Loading label="Loading tasks" />
      ) : tasks.isError ? (
        <ErrorState error={tasks.error} onRetry={() => void tasks.refetch()} />
      ) : tasks.data.items.length === 0 ? (
        <p className="muted">No open tasks.</p>
      ) : (
        <ul className="rows">
          {tasks.data.items.map((task) => (
            <li key={task.id} className="row">
              <div className="row-main">
                <span className="row-title">{task.title}</span>
                <span className="muted">Due {formatDateTime(task.due_at, timeZone)}</span>
              </div>
              {writable ? (
                <button type="button" className="button" disabled={complete.isPending} onClick={() => complete.mutate(task.id)}>
                  Done<span className="visually-hidden">: {task.title}</span>
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {writable ? (
        <form
          className="inline-form"
          onSubmit={(event) => {
            event.preventDefault();
            add.mutate();
          }}
        >
          <div className="field">
            <label htmlFor={titleId}>New task</label>
            <input id={titleId} required maxLength={300} value={title} onChange={(e) => setTitle(e.target.value)} />
          </div>
          <button type="submit" className="button" disabled={add.isPending}>
            Add task
          </button>
          {add.isError ? <ErrorState error={add.error} /> : null}
        </form>
      ) : null}
    </section>
  );
}

function Notes({ companyId, writable }: { companyId: string; writable: boolean }) {
  const { tenantId } = useAuth();
  const queryClient = useQueryClient();
  const [body, setBody] = useState("");
  const bodyId = useId();
  const add = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/notes", { body: { company_id: companyId, body } })),
    onSuccess: () => {
      setBody("");
      void queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, ["activities"]) });
    },
  });
  if (!writable) return null;
  return (
    <section className="panel" aria-labelledby="note-heading">
      <h2 id="note-heading">Add a note</h2>
      <form
        className="stack"
        onSubmit={(event) => {
          event.preventDefault();
          add.mutate();
        }}
      >
        <div className="field">
          <label htmlFor={bodyId}>Note</label>
          <textarea id={bodyId} required rows={3} maxLength={20000} value={body} onChange={(e) => setBody(e.target.value)} />
        </div>
        {add.isError ? <ErrorState error={add.error} /> : null}
        <button type="submit" className="button" disabled={add.isPending}>
          Save note
        </button>
      </form>
    </section>
  );
}

function Timeline({ companyId, timeZone }: { companyId: string; timeZone?: string }) {
  const activities = useTenantQuery(["activities", { company_id: companyId }], () =>
    unwrap(api.GET("/api/v1/activities", { params: { query: { company_id: companyId, limit: 100 } } })),
  );
  return (
    <section className="panel" aria-labelledby="timeline-heading">
      <h2 id="timeline-heading">History</h2>
      {activities.isPending ? (
        <Loading label="Loading history" />
      ) : activities.isError ? (
        <ErrorState error={activities.error} onRetry={() => void activities.refetch()} />
      ) : activities.data.items.length === 0 ? (
        <p className="muted">Nothing has happened yet.</p>
      ) : (
        <ol className="timeline">
          {activities.data.items.map((activity) => (
            <li key={activity.id}>
              <time dateTime={activity.occurred_at} className="muted">
                {formatDateTime(activity.occurred_at, timeZone)}
              </time>
              <span>{activity.summary}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
