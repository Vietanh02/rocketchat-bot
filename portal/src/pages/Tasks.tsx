import { useState } from "react";
import { api } from "../api/client";
import type { Task, TaskDraft, TaskRun } from "../api/types";
import { useAuth } from "../auth";
import { Banner, Card, Checkbox, Empty, Field, Loading, Pill, StatusPill } from "../components/ui";
import { browserTimezone, formatDateTime, formatRelative } from "../format";
import { errorMessage, useResource } from "../hooks";

const WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

const TIMEZONES = (() => {
  const supported = (Intl as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf;
  const zones = supported ? supported("timeZone") : ["UTC"];
  return zones.includes("UTC") ? zones : ["UTC", ...zones];
})();

function blank(): TaskDraft {
  return {
    title: "",
    owner_rc_user_id: null,
    owner_username: null,
    room_id: null,
    dm_username: null,
    schedule_kind: "daily",
    time_of_day: "09:00",
    days_of_week: "mon,tue,wed,thu,fri",
    cron_expr: null,
    run_at: null,
    timezone_name: browserTimezone(),
    action_type: "message",
    message: "",
    integration_id: null,
    integration_args: "",
    enabled: true,
  };
}

function toDraft(task: Task): TaskDraft {
  const {
    id: _i, created_via: _cv, next_run_at: _n, last_run_at: _l, last_status: _s,
    schedule_description: _sd, next_run_local: _nl, created_at: _c, updated_at: _u,
    ...draft
  } = task;
  return draft;
}

/** <input type="datetime-local"> needs a local, second-less value. */
function toLocalInput(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export default function TasksPage() {
  const { isAdmin } = useAuth();
  const tasks = useResource(api.listTasks, []);
  const integrations = useResource(api.listIntegrations, []);

  const [editing, setEditing] = useState<{ id: number | null; draft: TaskDraft } | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [runs, setRuns] = useState<{ task: Task; entries: TaskRun[] } | null>(null);

  function patch(changes: Partial<TaskDraft>) {
    if (!editing) return;
    setEditing({ ...editing, draft: { ...editing.draft, ...changes } });
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!editing) return;
    setBusy(true);
    setNotice(null);
    try {
      if (editing.id === null) await api.createTask(editing.draft);
      else await api.updateTask(editing.id, editing.draft);
      await tasks.reload();
      setEditing(null);
      setNotice({ kind: "ok", text: "Reminder saved and scheduled." });
    } catch (caught) {
      setNotice({ kind: "error", text: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }

  async function toggle(task: Task) {
    try {
      await api.updateTask(task.id, { enabled: !task.enabled });
      await tasks.reload();
    } catch (caught) {
      setNotice({ kind: "error", text: errorMessage(caught) });
    }
  }

  async function runNow(task: Task) {
    setNotice(null);
    try {
      const run = await api.runTask(task.id);
      setNotice({
        kind: run.status === "ok" ? "ok" : "error",
        text: `"${task.title}": ${run.status} — ${run.detail}`,
      });
      await tasks.reload();
    } catch (caught) {
      setNotice({ kind: "error", text: errorMessage(caught) });
    }
  }

  async function remove(task: Task) {
    if (!window.confirm(`Delete the reminder "${task.title}"?`)) return;
    try {
      await api.deleteTask(task.id);
      await tasks.reload();
    } catch (caught) {
      setNotice({ kind: "error", text: errorMessage(caught) });
    }
  }

  async function showRuns(task: Task) {
    try {
      setRuns({ task, entries: await api.listTaskRuns(task.id) });
    } catch (caught) {
      setNotice({ kind: "error", text: errorMessage(caught) });
    }
  }

  if (tasks.loading && !tasks.data) return <Loading />;
  if (tasks.error) return <Banner kind="error">{tasks.error}</Banner>;

  const days = (editing?.draft.days_of_week ?? "").split(",").filter(Boolean);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Reminders</h1>
          <p>
            Scheduled messages. Users can also create their own in chat with{" "}
            <code>/remind daily 09:00 stand-up</code>.
          </p>
        </div>
        {isAdmin && (
          <button className="primary" type="button" onClick={() => setEditing({ id: null, draft: blank() })}>
            New reminder
          </button>
        )}
      </div>

      {notice && <Banner kind={notice.kind}>{notice.text}</Banner>}

      {editing && (
        <Card title={editing.id === null ? "New reminder" : `Editing "${editing.draft.title}"`}>
          <form className="stack" onSubmit={save}>
            <div className="grid">
              <Field label="Title">
                <input
                  type="text"
                  required
                  value={editing.draft.title}
                  onChange={(event) => patch({ title: event.target.value })}
                />
              </Field>
              <Field label="DM this user" hint="Rocket.Chat username. Leave empty to use a room.">
                <input
                  type="text"
                  value={editing.draft.dm_username ?? ""}
                  onChange={(event) => patch({ dm_username: event.target.value || null })}
                />
              </Field>
              <Field label="Or post to room ID" hint="Takes precedence over the DM target.">
                <input
                  type="text"
                  value={editing.draft.room_id ?? ""}
                  onChange={(event) => patch({ room_id: event.target.value || null })}
                />
              </Field>
            </div>

            <div className="grid">
              <Field label="Repeats">
                <select
                  value={editing.draft.schedule_kind}
                  onChange={(event) =>
                    patch({ schedule_kind: event.target.value as TaskDraft["schedule_kind"] })
                  }
                >
                  <option value="daily">Every day</option>
                  <option value="weekly">On chosen weekdays</option>
                  <option value="cron">Cron expression</option>
                  <option value="once">Once</option>
                </select>
              </Field>

              {(editing.draft.schedule_kind === "daily" || editing.draft.schedule_kind === "weekly") && (
                <Field label="Time of day">
                  <input
                    type="time"
                    required
                    value={editing.draft.time_of_day ?? "09:00"}
                    onChange={(event) => patch({ time_of_day: event.target.value })}
                  />
                </Field>
              )}

              {editing.draft.schedule_kind === "cron" && (
                <Field label="Cron expression" hint="Five fields, e.g. */15 9-17 * * 1-5">
                  <input
                    type="text"
                    required
                    value={editing.draft.cron_expr ?? ""}
                    onChange={(event) => patch({ cron_expr: event.target.value })}
                  />
                </Field>
              )}

              {editing.draft.schedule_kind === "once" && (
                <Field label="Run at" hint="Interpreted in your browser's timezone.">
                  <input
                    type="datetime-local"
                    required
                    value={toLocalInput(editing.draft.run_at)}
                    onChange={(event) =>
                      patch({
                        run_at: event.target.value
                          ? new Date(event.target.value).toISOString()
                          : null,
                      })
                    }
                  />
                </Field>
              )}

              <Field label="Timezone" hint="Applies to the time of day and cron fields.">
                <select
                  value={editing.draft.timezone_name}
                  onChange={(event) => patch({ timezone_name: event.target.value })}
                >
                  {TIMEZONES.map((zone) => (
                    <option key={zone} value={zone}>
                      {zone}
                    </option>
                  ))}
                </select>
              </Field>
            </div>

            {editing.draft.schedule_kind === "weekly" && (
              <div>
                <label>Weekdays</label>
                <div className="row">
                  {WEEKDAYS.map((day) => (
                    <Checkbox
                      key={day}
                      label={day}
                      checked={days.includes(day)}
                      onChange={(value) => {
                        const next = value
                          ? [...days, day]
                          : days.filter((existing) => existing !== day);
                        patch({
                          days_of_week: WEEKDAYS.filter((d) => next.includes(d)).join(","),
                        });
                      }}
                    />
                  ))}
                </div>
              </div>
            )}

            <Field label="What should it do?">
              <select
                value={editing.draft.action_type}
                onChange={(event) =>
                  patch({ action_type: event.target.value as TaskDraft["action_type"] })
                }
              >
                <option value="message">Send a message</option>
                <option value="integration">Call an API and post the result</option>
              </select>
            </Field>

            {editing.draft.action_type === "message" ? (
              <Field
                label="Message"
                hint="Jinja. Available: {{ user.mention }}, {{ task.title }}, {{ now }}."
              >
                <textarea
                  value={editing.draft.message}
                  onChange={(event) => patch({ message: event.target.value })}
                />
              </Field>
            ) : (
              <div className="grid">
                <Field label="Integration">
                  <select
                    value={editing.draft.integration_id ?? ""}
                    onChange={(event) =>
                      patch({
                        integration_id: event.target.value ? Number(event.target.value) : null,
                      })
                    }
                  >
                    <option value="">Select an integration…</option>
                    {(integrations.data ?? []).map((integration) => (
                      <option key={integration.id} value={integration.id}>
                        {integration.name}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Arguments (JSON object)" hint={'{"city": "Hanoi"}'}>
                  <textarea
                    value={editing.draft.integration_args}
                    onChange={(event) => patch({ integration_args: event.target.value })}
                  />
                </Field>
              </div>
            )}

            <div className="row">
              <Checkbox
                label="Enabled"
                checked={editing.draft.enabled}
                onChange={(value) => patch({ enabled: value })}
              />
            </div>

            <div className="row">
              <button className="primary" type="submit" disabled={busy}>
                {busy ? "Saving…" : "Save reminder"}
              </button>
              <button type="button" onClick={() => setEditing(null)}>
                Cancel
              </button>
            </div>
          </form>
        </Card>
      )}

      {runs && (
        <Card
          title={`Run history — ${runs.task.title}`}
          action={
            <button type="button" onClick={() => setRuns(null)}>
              Close
            </button>
          }
        >
          {runs.entries.length === 0 ? (
            <Empty>This reminder has not fired yet.</Empty>
          ) : (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Status</th>
                    <th>Detail</th>
                    <th>Duration</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.entries.map((run) => (
                    <tr key={run.id}>
                      <td className="muted">{formatDateTime(run.ran_at)}</td>
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

      <Card title="Scheduled reminders">
        {(tasks.data ?? []).length === 0 ? (
          <Empty>No reminders yet.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Reminder</th>
                  <th>Schedule</th>
                  <th>Next run</th>
                  <th>Target</th>
                  <th>Last run</th>
                  <th>State</th>
                  {isAdmin && <th />}
                </tr>
              </thead>
              <tbody>
                {(tasks.data ?? []).map((task) => (
                  <tr key={task.id}>
                    <td>
                      <strong>{task.title}</strong>
                      <div className="muted">
                        #{task.id} · via {task.created_via}
                        {task.action_type === "integration" && " · API"}
                      </div>
                    </td>
                    <td className="muted">{task.schedule_description}</td>
                    <td>
                      {task.enabled ? (
                        <>
                          {task.next_run_local}
                          <div className="muted">{formatRelative(task.next_run_at)}</div>
                        </>
                      ) : (
                        <span className="muted">paused</span>
                      )}
                    </td>
                    <td className="muted">
                      {task.room_id ? <code>{task.room_id}</code> : `@${task.dm_username ?? task.owner_username ?? "?"}`}
                    </td>
                    <td>
                      {task.last_run_at ? (
                        <>
                          <StatusPill status={task.last_status} />
                          <div className="muted">{formatRelative(task.last_run_at)}</div>
                        </>
                      ) : (
                        <span className="muted">never</span>
                      )}
                    </td>
                    <td>{task.enabled ? <Pill kind="ok">on</Pill> : <Pill>off</Pill>}</td>
                    {isAdmin && (
                      <td>
                        <div className="row">
                          <button type="button" onClick={() => setEditing({ id: task.id, draft: toDraft(task) })}>
                            Edit
                          </button>
                          <button type="button" onClick={() => runNow(task)}>
                            Run now
                          </button>
                          <button type="button" onClick={() => showRuns(task)}>
                            History
                          </button>
                          <button type="button" onClick={() => toggle(task)}>
                            {task.enabled ? "Pause" : "Resume"}
                          </button>
                          <button className="danger" type="button" onClick={() => remove(task)}>
                            Delete
                          </button>
                        </div>
                      </td>
                    )}
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
