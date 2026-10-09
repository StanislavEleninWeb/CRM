import {
  type QueryKey,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from "@tanstack/react-query";
import { createContext, type ReactNode, useCallback, useContext, useMemo } from "react";

import { api, ApiError, type Schemas, unwrap } from "../api/client";

export type Me = Schemas["Me"];
export type Role = Schemas["Role"];

interface AuthValue {
  me: Me;
  /** The active workspace, or null while the user has none. */
  tenantId: string | null;
  can: (permission: string) => boolean;
  switchTenant: (tenantId: string) => Promise<void>;
  refresh: () => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthValue | null>(null);
export const ME_KEY = ["me"] as const;

export function useMeQuery() {
  return useQuery({
    queryKey: ME_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/auth/me")),
    retry: (count, error) => count < 2 && !(error instanceof ApiError && error.status < 500),
    staleTime: 60_000,
  });
}

export function AuthProvider({ me, children }: { me: Me; children: ReactNode }) {
  const queryClient = useQueryClient();

  const resetForTenantChange = useCallback(async () => {
    // Drop every cached response from the previous workspace. Query keys also carry
    // the workspace ID, so a late response can never be shown under another workspace.
    await queryClient.cancelQueries();
    queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== ME_KEY[0] });
    await queryClient.invalidateQueries({ queryKey: ME_KEY });
  }, [queryClient]);

  const value = useMemo<AuthValue>(() => {
    const permissions = new Set(me.active_tenant?.permissions ?? []);
    return {
      me,
      tenantId: me.active_tenant?.id ?? null,
      can: (permission) => permissions.has(permission),
      switchTenant: async (tenantId) => {
        await unwrap(api.POST("/api/v1/auth/switch-tenant", { body: { tenant_id: tenantId } }));
        await resetForTenantChange();
      },
      refresh: resetForTenantChange,
      signOut: async () => {
        await unwrap(api.POST("/api/v1/auth/logout"));
        queryClient.clear();
        window.location.assign("/");
      },
    };
  }, [me, queryClient, resetForTenantChange]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}

/** The cache key prefix for everything that belongs to the active workspace. */
export function tenantKey(tenantId: string | null, key: QueryKey): QueryKey {
  return ["tenant", tenantId, ...key];
}

/** A query scoped to the active workspace. Disabled while there is none. */
export function useTenantQuery<T>(
  key: QueryKey,
  queryFn: () => Promise<T>,
  options?: Omit<UseQueryOptions<T>, "queryKey" | "queryFn">,
) {
  const { tenantId } = useAuth();
  return useQuery<T>({
    ...options,
    queryKey: tenantKey(tenantId, key),
    queryFn,
    enabled: tenantId !== null && (options?.enabled ?? true),
  });
}
