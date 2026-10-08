import { useId, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { ErrorBoundary } from "./ErrorBoundary";

const NAV_ITEMS = [{ to: "/", label: "Today", end: true }];

export function AppShell() {
  const [menuOpen, setMenuOpen] = useState(false);
  const location = useLocation();
  const navId = useId();
  const workspaceId = useId();

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
          <select id={workspaceId} disabled defaultValue="">
            <option value="">No workspace</option>
          </select>
        </div>
      </header>
      <nav id={navId} className={menuOpen ? "sidebar open" : "sidebar"} aria-label="Main">
        <ul>
          {NAV_ITEMS.map((item) => (
            <li key={item.to}>
              <NavLink to={item.to} end={item.end} onClick={() => setMenuOpen(false)}>
                {item.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <main id="main" className="content" tabIndex={-1}>
        <ErrorBoundary key={location.pathname}>
          <Outlet />
        </ErrorBoundary>
      </main>
    </div>
  );
}
