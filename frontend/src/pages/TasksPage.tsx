import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api, unwrap } from "../api/client";
import { tenantKey, useAuth, useTenantQuery } from "../auth/AuthContext";
import { Pagination } from "../components/Pagination";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { formatDateTime } from "../lib/format";

const PAGE_SIZE = 50;

export function TasksPage() {
  const { tenantId, can, me } = useAuth();
  const queryClient = useQueryClient();
  const [mine, setMine] = useState(true);
  const [offset, setOffset] = useState(0);
  const query = { status: "open" as const, mine, limit: PAGE_SIZE, offset };
  const tasks = useTenantQuery(["tasks", query], () => unwrap(api.GET("/api/v1/tasks", { params: { query } })));
  const complete = useMutation({
    mutationFn: (taskId: string) =>
      unwrap(api.PATCH("/api/v1/tasks/{task_id}", { params: { path: { task_id: taskId } }, body: { status: "done" } })),
    onSettled: () => queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, ["tasks"]) }),
  });
  const [now] = useState(() => Date.now());

  return (
    <>
      <h1>Tasks</h1>
      <label className="check">
        <input
          type="checkbox"
          checked={mine}
          onChange={(event) => {
            setMine(event.target.checked);
            setOffset(0);
          }}
        />
        Only mine
      </label>
      {complete.isError ? <ErrorState error={complete.error} /> : null}
      {tasks.isPending ? (
        <Loading label="Loading tasks" />
      ) : tasks.isError ? (
        <ErrorState error={tasks.error} onRetry={() => void tasks.refetch()} />
      ) : tasks.data.items.length === 0 ? (
        <EmptyState title="No open tasks." />
      ) : (
        <>
          <ul className="rows panel">
            {tasks.data.items.map((task) => {
              const overdue = task.due_at !== null && new Date(task.due_at).getTime() < now;
              return (
                <li key={task.id} className="row">
                  <div className="row-main">
                    <span className="row-title">{task.title}</span>
                    <span className="muted">
                      {task.company_id ? <Link to={`/companies/${task.company_id}`}>{task.company_name}</Link> : null}
                      {task.company_id ? " · " : ""}
                      {task.due_at ? `Due ${formatDateTime(task.due_at, me.active_tenant?.timezone)}` : "No due date"}
                    </span>
                  </div>
                  {overdue ? <span className="badge badge-warn">Overdue</span> : null}
                  {can("crm.write") ? (
                    <button type="button" className="button" disabled={complete.isPending} onClick={() => complete.mutate(task.id)}>
                      Done<span className="visually-hidden">: {task.title}</span>
                    </button>
                  ) : null}
                </li>
              );
            })}
          </ul>
          <Pagination total={tasks.data.total} limit={PAGE_SIZE} offset={offset} onChange={setOffset} />
        </>
      )}
    </>
  );
}
