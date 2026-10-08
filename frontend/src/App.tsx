import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";

import { ApiError } from "./api/client";
import { AuthProvider, useAuth, useMeQuery } from "./auth/AuthContext";
import { AppShell } from "./components/AppShell";
import { ErrorState, Loading } from "./components/States";
import { AcceptInvitationPage } from "./pages/AcceptInvitationPage";
import { AccountPage } from "./pages/AccountPage";
import { CompaniesPage } from "./pages/CompaniesPage";
import { CompanyPage } from "./pages/CompanyPage";
import { DealsPage } from "./pages/DealsPage";
import { ImportPage } from "./pages/ImportPage";
import { LoginPage } from "./pages/LoginPage";
import { NewWorkspacePage } from "./pages/NewWorkspacePage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { ProspectPage } from "./pages/ProspectPage";
import { ProspectsPage } from "./pages/ProspectsPage";
import { TasksPage } from "./pages/TasksPage";
import { TeamPage } from "./pages/TeamPage";
import { TodayPage } from "./pages/TodayPage";

export function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        retry: (count, error) =>
          count < 2 && !(error instanceof ApiError && error.status >= 400 && error.status < 500),
      },
    },
  });
}

/** Pages that need a workspace send users without one to create it first. */
function RequireWorkspace({ children }: { children: React.ReactNode }) {
  const { tenantId } = useAuth();
  return tenantId === null ? <Navigate to="/workspaces/new" replace /> : children;
}

export function AppRoutes() {
  const me = useMeQuery();
  const location = useLocation();
  const signedOut = me.error instanceof ApiError && me.error.status === 401;

  if (location.pathname === "/invitations/accept") {
    if (me.isPending) return <Loading label="Loading" />;
    return <AcceptInvitationPage me={me.data ?? null} />;
  }
  if (me.isPending) return <Loading label="Loading" />;
  if (signedOut) return <LoginPage />;
  if (me.isError) return <ErrorState error={me.error} onRetry={() => void me.refetch()} />;

  return (
    <AuthProvider me={me.data}>
      <Routes>
        <Route element={<AppShell />}>
          <Route
            index
            element={
              <RequireWorkspace>
                <TodayPage />
              </RequireWorkspace>
            }
          />
          {(
            [
              ["companies", <CompaniesPage key="companies" />],
              ["companies/:companyId", <CompanyPage key="company" />],
              ["opportunities", <DealsPage key="deals" />],
              ["tasks", <TasksPage key="tasks" />],
              ["import", <ImportPage key="import" />],
              ["prospects", <ProspectsPage key="prospects" />],
              ["verification", <ProspectsPage key="verification" verificationOnly />],
              ["prospects/:leadId", <ProspectPage key="prospect" />],
            ] as const
          ).map(([path, element]) => (
            <Route key={path} path={path} element={<RequireWorkspace>{element}</RequireWorkspace>} />
          ))}
          <Route
            path="team"
            element={
              <RequireWorkspace>
                <TeamPage />
              </RequireWorkspace>
            }
          />
          <Route path="workspaces/new" element={<NewWorkspacePage />} />
          <Route path="account" element={<AccountPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
    </AuthProvider>
  );
}

export function App() {
  const [queryClient] = useState(createQueryClient);
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
