import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";

import { api, type Schemas, unwrap } from "../api/client";
import { type Role, tenantKey, useAuth, useTenantQuery } from "../auth/AuthContext";
import { EmptyState, ErrorState, Loading } from "../components/States";

const ROLE_LABELS: Record<Role, string> = {
  owner: "Owner",
  administrator: "Administrator",
  sales_manager: "Sales manager",
  representative: "Sales representative",
  read_only: "Read-only",
};
const ROLES = Object.keys(ROLE_LABELS) as Role[];

export function TeamPage() {
  const { can } = useAuth();
  return (
    <>
      <h1>Team</h1>
      <MembersSection />
      {can("members.manage") ? <InvitationsSection /> : null}
    </>
  );
}

function MembersSection() {
  const { tenantId, can, me } = useAuth();
  const queryClient = useQueryClient();
  const canManage = can("members.manage");
  const canManageOwners = can("owners.manage");
  const members = useTenantQuery(["members"], () => unwrap(api.GET("/api/v1/members")));
  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, ["members"]) });

  const changeRole = useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: Role }) =>
      unwrap(
        api.PATCH("/api/v1/members/{user_id}", {
          params: { path: { user_id: userId } },
          body: { role },
        }),
      ),
    onSettled: invalidate,
  });
  const remove = useMutation({
    mutationFn: (userId: string) =>
      unwrap(api.DELETE("/api/v1/members/{user_id}", { params: { path: { user_id: userId } } })),
    onSettled: invalidate,
  });

  if (members.isPending) return <Loading label="Loading members" />;
  if (members.isError)
    return <ErrorState error={members.error} onRetry={() => void members.refetch()} />;

  const actionError = changeRole.error ?? remove.error;
  return (
    <section className="panel" aria-labelledby="members-heading">
      <h2 id="members-heading">Members ({members.data.total})</h2>
      {actionError ? <ErrorState error={actionError} /> : null}
      <ul className="rows">
        {members.data.items.map((member) => {
          const editable = canManage && (member.role !== "owner" || canManageOwners);
          return (
            <li key={member.user_id} className="row">
              <div className="row-main">
                <span className="row-title">{member.display_name || member.email}</span>
                {member.display_name ? <span className="muted">{member.email}</span> : null}
              </div>
              {editable ? (
                <div className="row-actions">
                  <label className="visually-hidden" htmlFor={`role-${member.user_id}`}>
                    Role for {member.email}
                  </label>
                  <select
                    id={`role-${member.user_id}`}
                    value={member.role}
                    disabled={changeRole.isPending}
                    onChange={(event) =>
                      changeRole.mutate({
                        userId: member.user_id,
                        role: event.target.value as Role,
                      })
                    }
                  >
                    {ROLES.filter((role) => role !== "owner" || canManageOwners).map((role) => (
                      <option key={role} value={role}>
                        {ROLE_LABELS[role]}
                      </option>
                    ))}
                  </select>
                  {member.user_id !== me.user.id ? (
                    <button
                      type="button"
                      className="button"
                      disabled={remove.isPending}
                      onClick={() => {
                        if (window.confirm(`Remove ${member.email} from this workspace?`)) {
                          remove.mutate(member.user_id);
                        }
                      }}
                    >
                      Remove<span className="visually-hidden"> {member.email}</span>
                    </button>
                  ) : null}
                </div>
              ) : (
                <span className="badge">{ROLE_LABELS[member.role]}</span>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function InvitationsSection() {
  const { tenantId, can } = useAuth();
  const queryClient = useQueryClient();
  const emailId = useId();
  const roleId = useId();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("representative");
  const [created, setCreated] = useState<Schemas["InvitationCreated"] | null>(null);
  const [copied, setCopied] = useState(false);
  const invitations = useTenantQuery(["invitations"], () =>
    unwrap(api.GET("/api/v1/invitations")),
  );
  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: tenantKey(tenantId, ["invitations"]) });

  const create = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/invitations", { body: { email, role } })),
    onSuccess: (invitation) => {
      setCreated(invitation);
      setCopied(false);
      setEmail("");
      void invalidate();
    },
  });
  const revoke = useMutation({
    mutationFn: (id: string) =>
      unwrap(
        api.DELETE("/api/v1/invitations/{invitation_id}", {
          params: { path: { invitation_id: id } },
        }),
      ),
    onSettled: invalidate,
  });

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    create.mutate();
  }

  return (
    <section className="panel" aria-labelledby="invitations-heading">
      <h2 id="invitations-heading">Invitations</h2>
      <form onSubmit={onSubmit} className="inline-form">
        <div className="field">
          <label htmlFor={emailId}>Email</label>
          <input
            id={emailId}
            type="email"
            required
            value={email}
            autoComplete="off"
            onChange={(event) => setEmail(event.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor={roleId}>Role</label>
          <select id={roleId} value={role} onChange={(event) => setRole(event.target.value as Role)}>
            {ROLES.filter((option) => option !== "owner" || can("owners.manage")).map((option) => (
              <option key={option} value={option}>
                {ROLE_LABELS[option]}
              </option>
            ))}
          </select>
        </div>
        <button type="submit" className="button button-primary" disabled={create.isPending}>
          {create.isPending ? "Creating…" : "Create invitation"}
        </button>
      </form>
      {create.isError ? <ErrorState error={create.error} /> : null}
      {created ? (
        <div className="notice" role="status">
          <p>
            <strong>Send this link to {created.email}.</strong> It is shown only once and expires
            on {new Date(created.expires_at).toLocaleString()}.
          </p>
          <div className="inline-form">
            <input
              readOnly
              aria-label="Invitation link"
              value={created.accept_url}
              onFocus={(event) => event.target.select()}
            />
            <button
              type="button"
              className="button"
              onClick={() => {
                void navigator.clipboard?.writeText(created.accept_url).then(() => setCopied(true));
              }}
            >
              {copied ? "Copied" : "Copy link"}
            </button>
          </div>
        </div>
      ) : null}

      {invitations.isPending ? (
        <Loading label="Loading invitations" />
      ) : invitations.isError ? (
        <ErrorState error={invitations.error} onRetry={() => void invitations.refetch()} />
      ) : invitations.data.items.length === 0 ? (
        <EmptyState title="No invitations yet." />
      ) : (
        <ul className="rows">
          {invitations.data.items.map((invitation) => (
            <li key={invitation.id} className="row">
              <div className="row-main">
                <span className="row-title">{invitation.email}</span>
                <span className="muted">
                  {ROLE_LABELS[invitation.role]} · {invitation.status}
                </span>
              </div>
              {invitation.status === "pending" ? (
                <button
                  type="button"
                  className="button"
                  disabled={revoke.isPending}
                  onClick={() => revoke.mutate(invitation.id)}
                >
                  Revoke<span className="visually-hidden"> invitation for {invitation.email}</span>
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
