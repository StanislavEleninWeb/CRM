import { useMutation } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api, unwrap } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { ErrorState } from "../components/States";

export function NewWorkspacePage() {
  const { refresh, me } = useAuth();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const nameId = useId();
  const create = useMutation({
    mutationFn: (workspaceName: string) =>
      unwrap(api.POST("/api/v1/tenants", { body: { name: workspaceName } })),
    onSuccess: async () => {
      await refresh();
      navigate("/");
    },
  });

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (name.trim()) create.mutate(name.trim());
  }

  return (
    <div className="panel narrow">
      <h1>{me.tenants.length === 0 ? "Create your workspace" : "New workspace"}</h1>
      <p className="muted">
        A workspace holds one organisation&apos;s companies, contacts and history. You will be its
        owner.
      </p>
      <form onSubmit={onSubmit} className="stack">
        <div className="field">
          <label htmlFor={nameId}>Workspace name</label>
          <input
            id={nameId}
            value={name}
            maxLength={120}
            required
            autoComplete="organization"
            onChange={(event) => setName(event.target.value)}
          />
        </div>
        {create.isError ? <ErrorState error={create.error} /> : null}
        <button type="submit" className="button button-primary" disabled={create.isPending}>
          {create.isPending ? "Creating…" : "Create workspace"}
        </button>
      </form>
    </div>
  );
}
