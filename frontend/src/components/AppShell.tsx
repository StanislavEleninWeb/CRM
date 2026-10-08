import { useId, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { ErrorBoundary } from "./ErrorBoundary";
import { ErrorState } from "./States";

interface NavItem {
  to: string;
  label: string;
  end?: boolean;
  permission?: string;
}

const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Today", end: true },
  { to: "/companies", label: "Companies", permission: "crm.read" },
  { to: "/opportunities", label: "Opportunities", permission: "crm.read" },
  { to: "/tasks", label: "Tasks", permission: "crm.read" },
  { to: "/import", label: "Import", permission: "import.run" },
  { to: "/team", label: "Team", permission: "members.read" },
  { to: "/account", label: "Account" },
];

export function AppShell() {
  const { me, tenantId, can, switchTenant } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
  const [switchError, setSwitchError] = useState<unknown>(null);
  const [switching, setSwitching] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();
  const navId = useId();
  const workspaceId = useId();

  async function onSwitch(nextTenantId: string) {
    if (nextTenantId === "__new__") {
      navigate("/workspaces/new");
      return;
    }
    setSwitching(true);
    setSwitchError(null);
    try {
      await switchTenant(nextTenantId);
      navigate("/");
    } catch (error) {
      setSwitchError(error);
    } finally {
      setSwitching(false);
    }
  }

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <button
          type="button"
          className="button button-quiet menu-toggle"
          aria-expanded={menuOpen}
          aria-controls={navId}
          onClick={() => setMenuOpen((open) => !open)}
        >
          Menu
        </button>
        <span className="brand">SEWEB CRM</span>
        <div className="workspace">
          <label htmlFor={workspaceId} className="visually-hidden">
            Workspace
          </label>
          <select
            id={workspaceId}
            value={tenantId ?? ""}
            disabled={switching}
            onChange={(event) => void onSwitch(event.target.value)}
          >
            {tenantId === null ? <option value="">No workspace</option> : null}
            {me.tenants.map((tenant) => (
              <option key={tenant.id} value={tenant.id}>
                {tenant.name}
              </option>
            ))}
            <option value="__new__">New workspace…</option>
          </select>
        </div>
      </header>
      <nav id={navId} className={menuOpen ? "sidebar open" : "sidebar"} aria-label="Main">
        <ul>
          {NAV_ITEMS.filter((item) => !item.permission || can(item.permission)).map((item) => (
            <li key={item.to}>
              <NavLink to={item.to} end={item.end} onClick={() => setMenuOpen(false)}>
                {item.label}
              </NavLink>
            </li>
          ))}
        </ul>
        <p className="muted sidebar-user">{me.user.email}</p>
      </nav>
      <main id="main" className="content" tabIndex={-1}>
        {switchError ? <ErrorState error={switchError} /> : null}
        {/* Remount on workspace change so no component keeps state from the previous one. */}
        <ErrorBoundary key={`${tenantId}:${location.pathname}`}>
          <Outlet />
        </ErrorBoundary>
      </main>
    </div>
  );
}
