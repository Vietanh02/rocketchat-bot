import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { Banner, Card, Empty, Loading, Pill, Stat, StatusPill } from "../components/ui";
import { formatDateTime, formatRelative, truncate } from "../format";
import { errorMessage, usePoll, useResource } from "../hooks";
import { useAuth } from "../auth";

export default function DashboardPage() {
  const { isAdmin } = useAuth();
  const stats = useResource(api.dashboard, []);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  usePoll(
    useCallback(() => {
      void stats.reload();
    }, [stats.reload]),
    15_000,
  );

  async function restart() {
    setBusy(true);
    setNotice(null);
    try {
      const status = await api.restartBot();
      setNotice(
        status.connected
          ? `Reconnected as @${status.bot_username}.`
          : `Not connected: ${status.last_error ?? "unknown reason"}`,
      );
      await stats.reload();
    } catch (caught) {
      setNotice(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  if (stats.loading && !stats.data) return <Loading />;
  if (stats.error) return <Banner kind="error">{stats.error}</Banner>;
  if (!stats.data) return null;

  const { connection } = stats.data;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Dashboard</h1>
          <p>How the bot is doing right now.</p>
        </div>
        {isAdmin && (
          <button type="button" onClick={restart} disabled={busy}>
            {busy ? "Reconnecting…" : "Reconnect bot"}
          </button>
        )}
      </div>

      {notice && <Banner kind="warn">{notice}</Banner>}

      {!connection.connected && (
        <Banner kind="error">
          The bot is not connected to Rocket.Chat
          {connection.last_error ? `: ${connection.last_error}` : "."}{" "}
          <Link to="/settings">Check bot settings</Link>.
        </Banner>
      )}

      <div className="grid" style={{ marginTop: "1rem" }}>
        <Stat label="Commands enabled" value={`${stats.data.commands_enabled} / ${stats.data.commands_total}`} />
        <Stat label="Integrations" value={stats.data.integrations_total} />
        <Stat label="Reminders active" value={`${stats.data.tasks_enabled} / ${stats.data.tasks_total}`} />
        <Stat label="Messages (24h)" value={stats.data.messages_today} />
        <Stat label="Errors (24h)" value={stats.data.errors_today} />
        <Stat label="Timers scheduled" value={connection.tasks_scheduled} />
      </div>

      <Card title="Connection">
        <div className="table-scroll">
          <table>
            <tbody>
              <tr>
                <th>Status</th>
                <td>
                  {connection.connected ? (
                    <Pill kind="ok">connected</Pill>
                  ) : (
                    <Pill kind="error">disconnected</Pill>
                  )}{" "}
                  {connection.bot_username && <code>@{connection.bot_username}</code>}
                </td>
              </tr>
              <tr>
                <th>REST</th>
                <td><code>{connection.rest_url ?? "—"}</code></td>
              </tr>
              <tr>
                <th>Realtime</th>
                <td><code>{connection.ws_url ?? "—"}</code></td>
              </tr>
              <tr>
                <th>Scheduler</th>
                <td>
                  {connection.scheduler_running ? (
                    <Pill kind="ok">running</Pill>
                  ) : (
                    <Pill kind="error">stopped</Pill>
                  )}
                </td>
              </tr>
              <tr>
                <th>Replies sent</th>
                <td>{connection.messages_handled}</td>
              </tr>
              {connection.details.max_concurrency && (
                <tr>
                  <th>Handling now</th>
                  <td>
                    {connection.details.active_handlers ?? 0} of {connection.details.max_concurrency} slots
                    {Number(connection.details.waiting ?? 0) > 0 && (
                      <span className="muted"> · {connection.details.waiting} waiting</span>
                    )}
                  </td>
                </tr>
              )}
              {connection.last_error && (
                <tr>
                  <th>Last error</th>
                  <td className="muted">{connection.last_error}</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title="Next reminders" action={<Link to="/tasks">Manage</Link>}>
        {stats.data.upcoming_tasks.length === 0 ? (
          <Empty>No reminders scheduled.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Reminder</th>
                  <th>Schedule</th>
                  <th>Next run</th>
                  <th>Owner</th>
                </tr>
              </thead>
              <tbody>
                {stats.data.upcoming_tasks.map((task) => (
                  <tr key={task.id}>
                    <td>{task.title}</td>
                    <td className="muted">{task.schedule_description}</td>
                    <td>
                      {task.next_run_local}
                      <div className="muted">{formatRelative(task.next_run_at)}</div>
                    </td>
                    <td className="muted">{task.owner_username ?? task.dm_username ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Recent conversations" action={<Link to="/logs">All logs</Link>}>
        {stats.data.recent_messages.length === 0 ? (
          <Empty>No messages handled yet. Send the bot a direct message.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>From</th>
                  <th>Message</th>
                  <th>Command</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {stats.data.recent_messages.map((entry) => (
                  <tr key={entry.id}>
                    <td className="muted">{formatDateTime(entry.received_at)}</td>
                    <td>{entry.sender_username ?? "—"}</td>
                    <td>{truncate(entry.text_in, 60)}</td>
                    <td>{entry.matched_command ? <code>{entry.matched_command}</code> : <span className="muted">—</span>}</td>
                    <td><StatusPill status={entry.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
