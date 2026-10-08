import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "../api/client";
import { EmptyState, ErrorState, Loading } from "../components/States";

export function TodayPage() {
  const info = useQuery({
    queryKey: ["system", "info"],
    queryFn: () => unwrap(api.GET("/api/v1/system/info")),
  });
  const readiness = useQuery({
    queryKey: ["system", "readiness"],
    queryFn: () => unwrap(api.GET("/api/v1/system/readiness")),
    retry: false,
  });

  return (
    <>
      <h1>Today</h1>
      <EmptyState title="Nothing is due yet.">
        <p>Follow-ups, calls and replies that need attention will appear here.</p>
      </EmptyState>

      <section aria-labelledby="system-heading" className="panel">
        <h2 id="system-heading">System</h2>
        {info.isPending ? (
          <Loading label="Loading system details" />
        ) : info.isError ? (
          <ErrorState error={info.error} onRetry={() => void info.refetch()} />
        ) : (
          <dl className="facts">
            <div>
              <dt>Environment</dt>
              <dd>{info.data.environment}</dd>
            </div>
            <div>
              <dt>Default currency</dt>
              <dd>{info.data.default_currency}</dd>
            </div>
            <div>
              <dt>Default time zone</dt>
              <dd>{info.data.default_timezone}</dd>
            </div>
            <div>
              <dt>Services</dt>
              <dd>
                {readiness.isPending
                  ? "Checking…"
                  : readiness.isError
                    ? "Not ready"
                    : Object.entries(readiness.data.checks)
                        .map(([name, status]) => `${name}: ${status}`)
                        .join(", ")}
              </dd>
            </div>
          </dl>
        )}
      </section>
    </>
  );
}
