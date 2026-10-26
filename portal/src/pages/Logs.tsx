import { useCallback, useState } from "react";
import { api } from "../api/client";
import { Banner, Card, Empty, Loading, StatusPill } from "../components/ui";
import { formatDateTime } from "../format";
import { usePoll, useResource } from "../hooks";

type Tab = "messages" | "runs" | "audit";

const TABS: { key: Tab; label: string }[] = [
  { key: "messages", label: "Conversations" },
  { key: "runs", label: "Reminder runs" },
  { key: "audit", label: "Portal changes" },
];

export default function LogsPage() {
  const [tab, setTab] = useState<Tab>("messages");
  const [live, setLive] = useState(true);

  const messages = useResource(() => api.messageLogs(200), []);
  const runs = useResource(() => api.taskRunLogs(200), []);
  const audit = useResource(() => api.auditLogs(200), []);

  const active = tab === "messages" ? messages : tab === "runs" ? runs : audit;

  usePoll(
    useCallback(() => {
      if (live) void active.reload();
    }, [live, active.reload]),
    10_000,
  );

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Logs</h1>
          <p>Everything the bot handled, fired, or had changed underneath it.</p>
        </div>
        <div className="row">
          <label className="checkbox">
            <input type="checkbox" checked={live} onChange={(event) => setLive(event.target.checked)} />
            Auto-refresh
          </label>
          <button type="button" onClick={() => void active.reload()}>
            Refresh
          </button>
        </div>
      </div>

      <div className="row" style={{ marginBottom: "1rem" }}>
        {TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            className={tab === item.key ? "primary" : undefined}
            onClick={() => setTab(item.key)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {active.error && <Banner kind="error">{active.error}</Banner>}
      {active.loading && !active.data && <Loading />}

      {tab === "messages" && messages.data && (
        <Card>
          {messages.data.length === 0 ? (
            <Empty>No messages handled yet.</Empty>
          ) : (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>When</th>
                    <th>From</th>
                    <th>Received</th>
                    <th>Command</th>
                    <th>Replied</th>
                    <th>Status</th>
                    <th>Took</th>
                  </tr>
                </thead>
                <tbody>
                  {messages.data.map((entry) => (
                    <tr key={entry.id}>
                      <td className="muted">{formatDateTime(entry.received_at)}</td>
                      <td>
                        {entry.sender_username ?? "—"}
                        <div className="muted">{entry.room_type === "d" ? "DM" : "channel"}</div>
                      </td>
                      <td style={{ maxWidth: "22ch" }}>{entry.text_in}</td>
                      <td>
                        {entry.matched_command ? (
                          <code>{entry.matched_command}</code>
                        ) : (
                          <span className="muted">—</span>
                        )}
                      </td>
                      <td style={{ maxWidth: "28ch" }}>
                        {entry.text_out || <span className="muted">—</span>}
                        {entry.error && <div style={{ color: "var(--error)" }}>{entry.error}</div>}
                      </td>
                      <td><StatusPill status={entry.status} /></td>
                      <td className="muted">{entry.duration_ms != null ? `${entry.duration_ms}ms` : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {tab === "runs" && runs.data && (
        <Card>
          {runs.data.length === 0 ? (
            <Empty>No reminders have fired yet.</Empty>
          ) : (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Reminder</th>
                    <th>Status</th>
                    <th>Detail</th>
                    <th>Took</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.data.map((run) => (
                    <tr key={run.id}>
                      <td className="muted">{formatDateTime(run.ran_at)}</td>
                      <td>#{run.task_id}</td>
                      <td><StatusPill status={run.status} /></td>
                      <td>{run.detail}</td>
                      <td className="muted">{run.duration_ms != null ? `${run.duration_ms}ms` : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {tab === "audit" && audit.data && (
        <Card>
          {audit.data.length === 0 ? (
            <Empty>No portal changes recorded.</Empty>
          ) : (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Who</th>
                    <th>Action</th>
                    <th>Target</th>
                    <th>Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {audit.data.map((entry) => (
                    <tr key={entry.id}>
                      <td className="muted">{formatDateTime(entry.at)}</td>
                      <td>{entry.actor}</td>
                      <td><code>{entry.action}</code></td>
                      <td>
                        {entry.entity}
                        {entry.entity_id && ` #${entry.entity_id}`}
                      </td>
                      <td className="muted">{entry.detail}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </>
  );
}
