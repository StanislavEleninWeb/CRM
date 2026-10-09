import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import { api, loginUrl, unwrap } from "../api/client";
import { ME_KEY, type Me } from "../auth/AuthContext";
import { EmptyState, ErrorState, Loading } from "../components/States";

/** Reachable signed out: shows what the invitation is for, then asks the person to sign in. */
export function AcceptInvitationPage({ me }: { me: Me | null }) {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const queryClient = useQueryClient();
  const invitation = useQuery({
    queryKey: ["invitation", token],
    queryFn: () =>
      unwrap(api.GET("/api/v1/invitations/lookup", { params: { query: { token } } })),
    enabled: token.length >= 20,
    retry: false,
  });
  const accept = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/invitations/accept", { body: { token } })),
    onSuccess: async () => {
      queryClient.clear();
      await queryClient.invalidateQueries({ queryKey: ME_KEY });
      window.location.assign("/");
    },
  });

  let body;
  if (token.length < 20) {
    body = <EmptyState title="This invitation link is incomplete." />;
  } else if (invitation.isPending) {
    body = <Loading label="Checking invitation" />;
  } else if (invitation.isError) {
    body = <ErrorState error={invitation.error} />;
  } else if (invitation.data.status !== "pending") {
    body = <EmptyState title={`This invitation is ${invitation.data.status}.`} />;
  } else if (!me) {
    body = (
      <>
        <p>
          You have been invited to join <strong>{invitation.data.tenant_name}</strong>. Sign in as{" "}
          <strong>{invitation.data.email}</strong> to accept.
        </p>
        <a className="button button-primary" href={loginUrl()}>
          Sign in to accept
        </a>
      </>
    );
  } else if (me.user.email.toLowerCase() !== invitation.data.email.toLowerCase()) {
    body = (
      <EmptyState title="This invitation was sent to a different email address.">
        <p>
          You are signed in as {me.user.email}. Sign out and sign in as {invitation.data.email}.
        </p>
      </EmptyState>
    );
  } else {
    body = (
      <>
        <p>
          Join <strong>{invitation.data.tenant_name}</strong>?
        </p>
        {accept.isError ? <ErrorState error={accept.error} /> : null}
        <button
          type="button"
          className="button button-primary"
          disabled={accept.isPending}
          onClick={() => accept.mutate()}
        >
          {accept.isPending ? "Joining…" : "Accept invitation"}
        </button>
      </>
    );
  }

  return (
    <main className="centered">
      <div className="panel narrow">
        <h1>Workspace invitation</h1>
        {body}
      </div>
    </main>
  );
}
