import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { ErrorState, Loading } from "../components/States";

const SESSIONS_KEY = ["me", "sessions"] as const;

export function AccountPage() {
  const { me, signOut } = useAuth();
  const queryClient = useQueryClient();
  const sessions = useQuery({
    queryKey: SESSIONS_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/auth/sessions")),
  });
  const revokeOthers = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/auth/sessions/revoke-others")),
    onSettled: () => queryClient.invalidateQueries({ queryKey: SESSIONS_KEY }),
  });
  const signOutMutation = useMutation({ mutationFn: signOut });

  return (
    <>
      <h1>Account</h1>
      <section className="panel" aria-labelledby="profile-heading">
        <h2 id="profile-heading">Profile</h2>
        <dl className="facts">
          <div>
            <dt>Name</dt>
            <dd>{me.user.display_name || "—"}</dd>
          </div>
          <div>
            <dt>Email</dt>
            <dd>{me.user.email}</dd>
          </div>
          <div>
            <dt>Multi-factor sign-in</dt>
            <dd>{me.mfa_claimed ? "Reported by identity provider" : "Not reported"}</dd>
          </div>
        </dl>
        <button
          type="button"
          className="button"
          disabled={signOutMutation.isPending}
          onClick={() => signOutMutation.mutate()}
        >
          Sign out
        </button>
        {signOutMutation.isError ? <ErrorState error={signOutMutation.error} /> : null}
      </section>

      <section className="panel" aria-labelledby="sessions-heading">
        <h2 id="sessions-heading">Signed-in sessions</h2>
        {sessions.isPending ? (
          <Loading label="Loading sessions" />
        ) : sessions.isError ? (
          <ErrorState error={sessions.error} onRetry={() => void sessions.refetch()} />
        ) : (
          <>
            <ul className="rows">
              {sessions.data.items.map((session) => (
                <li key={session.id} className="row">
                  <div className="row-main">
                    <span className="row-title">
                      {session.current ? "This device" : "Another device"}
                    </span>
                    <span className="muted">
                      Last active {new Date(session.last_seen_at).toLocaleString()}
                      {session.user_agent ? ` · ${session.user_agent.slice(0, 60)}` : ""}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
            {sessions.data.items.length > 1 ? (
              <button
                type="button"
                className="button"
                disabled={revokeOthers.isPending}
                onClick={() => revokeOthers.mutate()}
              >
                Sign out other devices
              </button>
            ) : null}
            {revokeOthers.isError ? <ErrorState error={revokeOthers.error} /> : null}
          </>
        )}
      </section>
    </>
  );
}
