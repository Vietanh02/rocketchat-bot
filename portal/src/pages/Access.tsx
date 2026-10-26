import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { AccessCheckResult, AccessEntry, AccessMode } from "../api/types";
import { useAuth } from "../auth";
import { Banner, Card, Empty, Field, Loading, Pill } from "../components/ui";
import { formatDateTime } from "../format";
import { errorMessage, useResource } from "../hooks";

const MODES: { value: AccessMode; label: string; hint: string }[] = [
  {
    value: "everyone",
    label: "Everyone",
    hint: "Anyone in the workspace can use the bot. The list below is kept but not applied.",
  },
  {
    value: "allowlist",
    label: "Only the users on the list",
    hint: "Everyone else is refused.",
  },
  {
    value: "blocklist",
    label: "Everyone except the users on the list",
    hint: "Listed users are refused; everyone else can use the bot.",
  },
];

const LIST_TITLE: Record<AccessMode, string> = {
  everyone: "Access list (not applied while the bot is open to everyone)",
  allowlist: "Allowed users",
  blocklist: "Blocked users",
};

export default function AccessPage() {
  const { isAdmin } = useAuth();
  const config = useResource(api.getConfig, []);
  const entries = useResource(api.listAccess, []);

  const [mode, setMode] = useState<AccessMode>("everyone");
  const [usernames, setUsernames] = useState("");
  const [note, setNote] = useState("");
  const [probe, setProbe] = useState("");
  const [probeResult, setProbeResult] = useState<AccessCheckResult | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "error" | "warn"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!config.data) return;
    setMode(config.data.access_mode);
  }, [config.data]);

  if ((config.loading && !config.data) || (entries.loading && !entries.data)) return <Loading />;
  if (config.error) return <Banner kind="error">{config.error}</Banner>;
  if (entries.error) return <Banner kind="error">{entries.error}</Banner>;

  const list = entries.data ?? [];
  const dirty = config.data !== null && mode !== config.data.access_mode;

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setNotice(null);
    try {
      await action();
    } catch (caught) {
      setNotice({ kind: "error", text: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }

  const savePolicy = (event: React.FormEvent) => {
    event.preventDefault();
    void run(async () => {
      const saved = await api.updateConfig({ access_mode: mode });
      config.setData(saved);
      setProbeResult(null);
      setNotice({ kind: "ok", text: "Access policy saved. It applies to the next message." });
    });
  };

  const add = (event: React.FormEvent) => {
    event.preventDefault();
    void run(async () => {
      const added = await api.addAccess(usernames, note);
      await entries.reload();
      setUsernames("");
      setNote("");
      setProbeResult(null);
      setNotice(
        added.length
          ? { kind: "ok", text: `Added ${added.map((entry) => `@${entry.username}`).join(", ")}.` }
          : { kind: "warn", text: "Those users are already on the list." },
      );
    });
  };

  const remove = (entry: AccessEntry) =>
    void run(async () => {
      await api.deleteAccess(entry.id);
      await entries.reload();
      setProbeResult(null);
      setNotice({ kind: "ok", text: `Removed @${entry.username}.` });
    });

  const check = (event: React.FormEvent) => {
    event.preventDefault();
    void run(async () => setProbeResult(await api.checkAccess(probe)));
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Access</h1>
          <p>Choose which Rocket.Chat users the bot answers.</p>
        </div>
      </div>

      {notice && <Banner kind={notice.kind}>{notice.text}</Banner>}
      {!isAdmin && <Banner kind="warn">You have read-only access; changes cannot be saved.</Banner>}
      {config.data?.access_mode === "allowlist" && list.length === 0 && (
        <Banner kind="warn">The allowlist is empty, so the bot refuses everyone.</Banner>
      )}

      <Card title="Who can use the bot">
        <form className="stack" onSubmit={savePolicy}>
          <Field label="Policy" hint={MODES.find((item) => item.value === mode)?.hint}>
            <select
              value={mode}
              disabled={!isAdmin}
              onChange={(event) => setMode(event.target.value as AccessMode)}
            >
              {MODES.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </Field>
          <p className="muted">
            The bot never replies to refused users. Their messages show as <code>denied</code> on the Logs page.
          </p>
          {isAdmin && (
            <div className="row">
              <button className="primary" type="submit" disabled={busy || !dirty}>
                Save policy
              </button>
            </div>
          )}
        </form>
      </Card>

      <Card title={LIST_TITLE[config.data?.access_mode ?? "everyone"]}>
        <div className="stack">
          {isAdmin && (
            <form className="grid" onSubmit={add}>
              <Field label="Usernames" hint="Separate several with commas, spaces or new lines. A leading @ is fine.">
                <textarea
                  value={usernames}
                  placeholder="@an, bo, cuong"
                  onChange={(event) => setUsernames(event.target.value)}
                />
              </Field>
              <div className="stack">
                <Field label="Note (optional)">
                  <input
                    type="text"
                    value={note}
                    placeholder="e.g. pilot team"
                    onChange={(event) => setNote(event.target.value)}
                  />
                </Field>
                <div className="row">
                  <button className="primary" type="submit" disabled={busy || !usernames.trim()}>
                    Add to list
                  </button>
                </div>
              </div>
            </form>
          )}

          {list.length === 0 ? (
            <Empty>No users on the list.</Empty>
          ) : (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>User</th>
                    <th>Note</th>
                    <th>Added</th>
                    {isAdmin && <th />}
                  </tr>
                </thead>
                <tbody>
                  {list.map((entry) => (
                    <tr key={entry.id}>
                      <td>@{entry.username}</td>
                      <td className="muted">{entry.note || "—"}</td>
                      <td className="muted">{formatDateTime(entry.created_at)}</td>
                      {isAdmin && (
                        <td>
                          <button
                            type="button"
                            className="danger"
                            disabled={busy}
                            onClick={() => remove(entry)}
                          >
                            Remove
                          </button>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </Card>

      <Card title="Check a user">
        <form className="row" onSubmit={check}>
          <input
            type="text"
            value={probe}
            placeholder="username"
            aria-label="Username to check"
            onChange={(event) => setProbe(event.target.value)}
          />
          <button type="submit" disabled={busy || !probe.trim()}>
            Check
          </button>
          {probeResult && (
            <span>
              <Pill kind={probeResult.allowed ? "ok" : "error"}>
                {probeResult.allowed ? "allowed" : "refused"}
              </Pill>{" "}
              @{probeResult.username}: {probeResult.reason}
              {dirty && " (saved policy; you have unsaved changes above)"}
            </span>
          )}
        </form>
      </Card>
    </div>
  );
}
