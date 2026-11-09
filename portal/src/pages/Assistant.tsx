import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { AgentConfig, Percentiles, QueueMode } from "../api/types";
import { useAuth } from "../auth";
import { Banner, Card, Checkbox, Empty, Field, Loading, Stat, StatusPill } from "../components/ui";
import { formatDateTime, truncate } from "../format";
import { errorMessage, useResource } from "../hooks";

const ms = (value: number | null | undefined) =>
  value == null ? "—" : value >= 10000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`;

const spread = (p: Percentiles) => `${ms(p.p50)} · ${ms(p.p95)}`;

export default function AssistantPage() {
  const { isAdmin } = useAuth();
  const config = useResource(api.getAgent, []);
  const [hours, setHours] = useState(24);
  const stats = useResource(() => api.agentStats(hours), [hours]);
  const [form, setForm] = useState<AgentConfig | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (config.data) setForm(config.data);
  }, [config.data]);

  if (config.loading && !form) return <Loading />;
  if (config.error) return <Banner kind="error">{config.error}</Banner>;
  if (!form) return null;

  const set = <K extends keyof AgentConfig>(key: K, value: AgentConfig[K]) =>
    setForm({ ...form, [key]: value });

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setNotice(null);
    try {
      config.setData(await api.updateAgent(form!));
      setNotice({ kind: "ok", text: "Saved." });
    } catch (caught) {
      setNotice({ kind: "error", text: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={save}>
      <div className="page-head">
        <div>
          <h1>Assistant</h1>
          <p>
            How conversations with the <Link to="/llm">language model</Link> are run, and how fast
            the answers were.
          </p>
        </div>
        {isAdmin && (
          <div className="row">
            <button className="primary" type="submit" disabled={busy}>
              {busy ? "Saving…" : "Save"}
            </button>
          </div>
        )}
      </div>

      {notice && <Banner kind={notice.kind}>{notice.text}</Banner>}
      {!isAdmin && <Banner kind="warn">You have read-only access; changes cannot be saved.</Banner>}

      <Card
        title="Speed"
        action={
          <select value={hours} onChange={(event) => setHours(Number(event.target.value))}>
            <option value={1}>Last hour</option>
            <option value={24}>Last 24 hours</option>
            <option value={24 * 7}>Last 7 days</option>
          </select>
        }
      >
        {stats.error && <Banner kind="error">{stats.error}</Banner>}
        {stats.data && stats.data.runs === 0 && <Empty>No answers from the model in this period.</Empty>}
        {stats.data && stats.data.runs > 0 && (
          <>
            <p className="hint">Median · 95th percentile, over {stats.data.runs} answers.</p>
            <div className="grid">
              <Stat label="Reaction shown" value={spread(stats.data.ack_ms)} />
              <Stat label="First words visible" value={spread(stats.data.ttft_ms)} />
              <Stat label="Answer complete" value={spread(stats.data.total_ms)} />
              <Stat label="Bot's own work" value={spread(stats.data.overhead_ms)} />
              <Stat label="Timeouts" value={stats.data.timeouts} />
              <Stat label="Errors" value={stats.data.errors} />
            </div>
            <p className="hint">
              “Bot's own work” is the total minus waiting for the model, tools, Rocket.Chat and the
              database.
            </p>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Status</th>
                    <th>Kind</th>
                    <th>Reaction</th>
                    <th>First words</th>
                    <th>Total</th>
                    <th>Own work</th>
                    <th>Tools</th>
                  </tr>
                </thead>
                <tbody>
                  {stats.data.recent.map((run) => (
                    <tr key={run.id} title={run.error ?? undefined}>
                      <td>{formatDateTime(run.started_at)}</td>
                      <td>
                        <StatusPill status={run.status} />
                      </td>
                      <td className="muted">
                        {run.intent ?? "—"}
                        {run.kb && run.kb !== "none" && ` · ${run.kb === "base" ? "knowledge base" : "chat document"}`}
                      </td>
                      <td>{ms(run.ack_ms)}</td>
                      <td>{ms(run.ttft_ms)}</td>
                      <td>{ms(run.total_ms)}</td>
                      <td>{ms(run.overhead_ms)}</td>
                      <td>
                        {run.tools.length
                          ? truncate(run.tools.map((t) => `${t.name} ${ms(t.ms)}`).join(", "), 60)
                          : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </Card>

      <Card title="Conversations">
        <div className="stack">
          <div className="grid">
            <Field label="Reaction while thinking" hint="Put on the question at once, removed once answered. Empty: none.">
              <input
                type="text"
                placeholder=":eyes:"
                value={form.ack_reaction}
                disabled={!isAdmin}
                onChange={(event) => set("ack_reaction", event.target.value)}
              />
            </Field>
            <Field label="Deadline (seconds)" hint="A turn never runs longer; what was written by then is posted.">
              <input
                type="number"
                min={5}
                max={600}
                value={form.turn_deadline_seconds}
                disabled={!isAdmin}
                onChange={(event) => set("turn_deadline_seconds", Number(event.target.value))}
              />
            </Field>
            <Field label="Messages sent while busy">
              <select
                value={form.queue_mode}
                disabled={!isAdmin}
                onChange={(event) => set("queue_mode", event.target.value as QueueMode)}
              >
                <option value="followup">Answer each in turn</option>
                <option value="collect">Answer them together</option>
              </select>
            </Field>
          </div>
          <div className="grid">
            <Field label="Start afresh after (minutes idle)" hint="0: never. Users can always send “new”.">
              <input
                type="number"
                min={0}
                value={form.idle_reset_minutes}
                disabled={!isAdmin}
                onChange={(event) => set("idle_reset_minutes", Number(event.target.value))}
              />
            </Field>
            <Field
              label="Summarize after (messages)"
              hint="Older messages are folded into a summary, after the reply. 0: never."
            >
              <input
                type="number"
                min={0}
                value={form.summarize_after_messages}
                disabled={!isAdmin}
                onChange={(event) => set("summarize_after_messages", Number(event.target.value))}
              />
            </Field>
            <Field label="Fast model" hint="A cheaper model on the same gateway, for summaries. Empty: the main model.">
              <input
                type="text"
                value={form.fast_model}
                disabled={!isAdmin}
                onChange={(event) => set("fast_model", event.target.value)}
              />
            </Field>
          </div>
          <Checkbox
            label="Run the tools of one step at the same time"
            checked={form.parallel_tools}
            disabled={!isAdmin}
            onChange={(value) => set("parallel_tools", value)}
          />
        </div>
      </Card>

      <Card title="Jobs">
        <div className="stack">
          <p className="hint" style={{ marginTop: 0 }}>
            Work that takes several steps runs as a <Link to="/jobs">job</Link>: each step is a turn of
            its own, a checklist in the conversation shows progress, and the answer is posted at the end.
            People can keep chatting meanwhile, and use <code>job list</code>, <code>job status</code>{" "}
            and <code>job cancel</code>.
          </p>
          <Checkbox
            label="Let the assistant start jobs"
            checked={form.jobs_enabled}
            disabled={!isAdmin}
            onChange={(value) => set("jobs_enabled", value)}
          />
          <div className="grid">
            <Field
              label="Go on as a job after (seconds)"
              hint="A reply still being worked out by then is split into steps by the fast model. 0: never; the reply is cut off at the deadline."
            >
              <input
                type="number"
                min={0}
                max={600}
                value={form.job_after_seconds}
                disabled={!isAdmin || !form.jobs_enabled}
                onChange={(event) => set("job_after_seconds", Number(event.target.value))}
              />
            </Field>
            <Field label="Steps per job (most)">
              <input
                type="number"
                min={1}
                max={12}
                value={form.job_max_phases}
                disabled={!isAdmin || !form.jobs_enabled}
                onChange={(event) => set("job_max_phases", Number(event.target.value))}
              />
            </Field>
            <Field label="Jobs per person at once">
              <input
                type="number"
                min={1}
                max={20}
                value={form.jobs_per_user}
                disabled={!isAdmin || !form.jobs_enabled}
                onChange={(event) => set("jobs_per_user", Number(event.target.value))}
              />
            </Field>
          </div>
        </div>
      </Card>
    </form>
  );
}
