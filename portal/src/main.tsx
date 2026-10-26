import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import "./index.css";
import { AuthProvider, useAuth } from "./auth";
import Layout from "./components/Layout";
import { Loading } from "./components/ui";
import AccessPage from "./pages/Access";
import CommandsPage from "./pages/Commands";
import DashboardPage from "./pages/Dashboard";
import IntegrationsPage from "./pages/Integrations";
import LoginPage from "./pages/Login";
import LogsPage from "./pages/Logs";
import SettingsPage from "./pages/Settings";
import TasksPage from "./pages/Tasks";

function App() {
  const { user, ready } = useAuth();

  if (!ready) return <Loading />;
  if (!user) return <LoginPage />;

  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/commands" element={<CommandsPage />} />
        <Route path="/integrations" element={<IntegrationsPage />} />
        <Route path="/tasks" element={<TasksPage />} />
        <Route path="/access" element={<AccessPage />} />
        <Route path="/logs" element={<LogsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <App />
      </AuthProvider>
    </BrowserRouter>
  </StrictMode>,
);
