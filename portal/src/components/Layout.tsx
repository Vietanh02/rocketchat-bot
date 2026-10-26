import { useCallback } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../auth";
import { usePoll, useResource } from "../hooks";

const NAV = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/commands", label: "Commands" },
  { to: "/integrations", label: "Integrations" },
  { to: "/tasks", label: "Reminders" },
  { to: "/access", label: "Access" },
  { to: "/logs", label: "Logs" },
  { to: "/settings", label: "Bot settings" },
];

export default function Layout() {
  const { user, logout } = useAuth();
  const status = useResource(api.getStatus, []);

  // Keep the connection light in the sidebar fresh without a manual refresh.
  usePoll(
    useCallback(() => {
      void status.reload();
    }, [status.reload]),
    15_000,
  );

  const connected = status.data?.connected ?? false;
  const dotClass = status.loading ? "" : connected ? "on" : "off";

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className={`brand-dot ${dotClass}`} aria-hidden />
          <span>Rocket.Chat Bot</span>
        </div>

        <nav>
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end}>
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-foot">
          <div>
            {connected
              ? `Connected as @${status.data?.bot_username ?? "?"}`
              : "Bot not connected"}
          </div>
          <div>
            {user?.email}
            {user?.role === "viewer" && " (read-only)"}
          </div>
          <button type="button" onClick={logout}>
            Sign out
          </button>
        </div>
      </aside>

      <main>
        <Outlet />
      </main>
    </div>
  );
}
