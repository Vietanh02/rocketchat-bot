import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Job } from "../api/types";
import { useAuth } from "../auth";
import { Banner, Card, Empty, Loading, Pill, Stat } from "../components/ui";
import { formatDateTime, formatRelative, truncate } from "../format";
import { errorMessage, usePoll, useResource } from "../hooks";

const STATUS: Record<string, { label: string; kind?: "ok" | "warn" | "error" }> = {
  planned: { label: "waiting" },
  running: { label: "running", kind: "warn" },
  done: { label: "done", kind: "ok" },
  failed: { label: "failed", kind: "error" },
  cancelled: { label: "cancelled" },
};

const STEP_ICON: Record<string, string> = { waiting: "▫️", running: "⏳", done: "✅", failed: "❌" };

function duration(job: Job): string {
  if (!job.started_at) return "—";
  const end = job.ended_at ? new Date(job.ended_at) : new Date();
  const seconds = Math.max(0, Math.round((end.getTime() - new Date(job.started_at).getTime()) / 1000));
  return seconds >= 60 ? `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s` : `${seconds}s`;
}

export default function JobsPage() {
  const { isAdmin } = useAuth();
  const jobs = useResource(api.listJobs, []);
  const [open, setOpen] = useState<number | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const all = jobs.data ?? [];
  const underWay = all.filter((job) => job.status === "running" || job.status === "planned");
  // Keep running jobs fresh.
  usePoll(() => {
    if (underWay.length > 0) void jobs.reload();
  }, 3000);

  const cancel = (job: Job) =>
    window.confirm(`Stop job #${job.id} (${job.title})?`) &&
    void api
      .cancelJob(job.id)
      .then(() => {
        void jobs.reload();
        setNotice({ kind: "ok", text: `Stopped job #${job.id}.` });
      })
      .catch((caught) => setNotice({ kind: "error", text: errorMessage(caught) }));

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Jobs</h1>
          <p>
            Work the assistant does in several steps, each a turn of its own. It starts one when a
            request needs more than one reply, or when a reply runs past the time set on the{" "}
            <Link to="/assistant">Assistant</Link> page. In chat, people use <code>job list</code>,{" "}
            <code>job status &lt;id&gt;</code> and <code>job cancel &lt;id&gt;</code>.
          </p>
        </div>
        <div className="row">
          <button type="button" onClick={() => void jobs.reload()}>
            Refresh
          </button>
        </div>
      </div>

      {notice && <Banner kind={notice.kind}>{notice.text}</Banner>}

      <Card title="State">
        <div className="grid">
          <Stat label="Under way" value={underWay.length} />
          <Stat label="Done" value={all.filter((job) => job.status === "done").length} />
          <Stat label="Failed" value={all.filter((job) => job.status === "failed").length} />
          <Stat label="Cancelled" value={all.filter((job) => job.status === "cancelled").length} />
        </div>
      </Card>

      <Card title="Recent jobs">
        {jobs.loading && !jobs.data ? (
          <Loading />
        ) : jobs.error ? (
          <Banner kind="error">{jobs.error}</Banner>
        ) : all.length === 0 ? (
          <Empty>No jobs yet.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Job</th>
                  <th>Status</th>
                  <th>Steps</th>
                  <th>Started</th>
                  <th>Took</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {all.map((job) => {
                  const done = job.phases.filter((phase) => phase.status === "done").length;
                  const shown = open === job.id;
                  return [
                    <tr key={job.id}>
                      <td>
                        <strong>#{job.id}</strong> {job.title}
                        <div className="hint">
                          {job.owner_username ? `@${job.owner_username}` : "—"} ·{" "}
                          {job.started_by === "deadline" ? "reply ran long" : "started by the model"}
                        </div>
                      </td>
                      <td>
                        <Pill kind={STATUS[job.status]?.kind}>{STATUS[job.status]?.label ?? job.status}</Pill>
                        {job.error && <div className="hint">{truncate(job.error, 120)}</div>}
                      </td>
                      <td>
                        {done}/{job.phases.length}
                      </td>
                      <td className="muted" title={formatDateTime(job.created_at)}>
                        {formatRelative(job.created_at)}
                      </td>
                      <td className="muted">{duration(job)}</td>
                      <td>
                        <div className="row">
                          <button type="button" onClick={() => setOpen(shown ? null : job.id)}>
                            {shown ? "Hide" : "Details"}
                          </button>
                          {isAdmin && (job.status === "running" || job.status === "planned") && (
                            <button type="button" className="danger" onClick={() => cancel(job)}>
                              Stop
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>,
                    shown && (
                      <tr key={`${job.id}-details`}>
                        <td colSpan={6}>
                          <div className="stack">
                            <div>
                              <label>Request</label>
                              <pre>{job.question}</pre>
                            </div>
                            {job.phases.map((phase) => (
                              <div key={phase.ord}>
                                <label>
                                  {STEP_ICON[phase.status] ?? ""} Step {phase.ord}: {phase.goal}
                                  {phase.attempts > 1 && <span className="hint"> ({phase.attempts} tries)</span>}
                                </label>
                                {phase.output && <pre>{phase.output}</pre>}
                                {phase.error && <Banner kind="error">{phase.error}</Banner>}
                              </div>
                            ))}
                          </div>
                        </td>
                      </tr>
                    ),
                  ];
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
