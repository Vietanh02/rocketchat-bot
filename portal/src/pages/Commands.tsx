import { useState } from "react";
import { api } from "../api/client";
import type { Command, CommandDraft, Integration } from "../api/types";
import { useAuth } from "../auth";
import { Banner, Card, Checkbox, Empty, Field, Loading, Pill } from "../components/ui";
import { errorMessage, useResource } from "../hooks";

const BLANK: CommandDraft = {
  name: "",
  description: "",
  trigger_type: "keyword",
  pattern: "",
  match_mode: "prefix",
  case_sensitive: false,
  priority: 100,
  enabled: true,
  show_in_help: true,
  response_type: "static",
  static_response: "",
  builtin_name: null,
  integration_id: null,
  script_name: null,
  script_timeout_seconds: 30,
};

function toDraft(command: Command): CommandDraft {
  const { id: _id, created_at: _c, updated_at: _u, ...draft } = command;
  return draft;
}

function triggerHelp(draft: CommandDraft): string {
  if (draft.trigger_type === "keyword") {
    const word = draft.pattern || "word";
    if (draft.match_mode === "prefix") {
      const target = draft.response_type === "script" ? "the script's arguments" : "{{ args }}";
      return `Fires on "${word} …". Anything after the word becomes ${target}. In channels, mention the bot first: "@bot ${word} …".`;
    }
    const mode = {
      exact: "the whole message equals the pattern",
      prefix: "",
      contains: "the pattern appears anywhere in the message",
    }[draft.match_mode];
    return `Fires when ${mode}.`;
  }
  return "Fires when the regex matches. Named groups become {{ groups.name }} and bind to integration parameters.";
}

export default function CommandsPage() {
  const { isAdmin } = useAuth();
  const commands = useResource(api.listCommands, []);
  const integrations = useResource(api.listIntegrations, []);
  const builtins = useResource(api.listBuiltins, []);
  const scripts = useResource(api.listScripts, []);

  const [editing, setEditing] = useState<{ id: number | null; draft: CommandDraft } | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const [probe, setProbe] = useState("");
  const [probeResult, setProbeResult] = useState<string | null>(null);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!editing) return;
    setBusy(true);
    setNotice(null);
    try {
      if (editing.id === null) await api.createCommand(editing.draft);
      else await api.updateCommand(editing.id, editing.draft);
      await commands.reload();
      setEditing(null);
      setNotice({ kind: "ok", text: "Command saved. It is live immediately." });
    } catch (caught) {
      setNotice({ kind: "error", text: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }

  async function toggle(command: Command) {
    try {
      await api.updateCommand(command.id, { enabled: !command.enabled });
      await commands.reload();
    } catch (caught) {
      setNotice({ kind: "error", text: errorMessage(caught) });
    }
  }

  async function remove(command: Command) {
    if (!window.confirm(`Delete the command "${command.name}"?`)) return;
    try {
      await api.deleteCommand(command.id);
      await commands.reload();
    } catch (caught) {
      setNotice({ kind: "error", text: errorMessage(caught) });
    }
  }

  async function runProbe() {
    setProbeResult(null);
    try {
      const result = await api.testCommand(probe, false);
      if (!result.matched) setProbeResult("No command matched that message.");
      else
        setProbeResult(
          `Matched ${result.command_name}` +
            (result.args ? ` (args: ${result.args})` : "") +
            `\n\n${result.error ?? result.reply ?? "(no reply)"}`,
        );
    } catch (caught) {
      setProbeResult(errorMessage(caught));
    }
  }

  if (commands.loading && !commands.data) return <Loading />;
  if (commands.error) return <Banner kind="error">{commands.error}</Banner>;

  const integrationList: Integration[] = integrations.data ?? [];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Commands</h1>
          <p>What the bot listens for and how it answers. Changes apply without a restart.</p>
        </div>
        {isAdmin && (
          <button
            className="primary"
            type="button"
            onClick={() => setEditing({ id: null, draft: { ...BLANK } })}
          >
            New command
          </button>
        )}
      </div>

      {notice && <Banner kind={notice.kind}>{notice.text}</Banner>}

      {editing && (
        <Card title={editing.id === null ? "New command" : `Editing "${editing.draft.name}"`}>
          <form className="stack" onSubmit={save}>
            <div className="grid">
              <Field label="Name" hint="Internal identifier; must be unique.">
                <input
                  type="text"
                  required
                  value={editing.draft.name}
                  onChange={(event) =>
                    setEditing({ ...editing, draft: { ...editing.draft, name: event.target.value } })
                  }
                />
              </Field>
              <Field label="Description" hint="Shown in the bot's help message.">
                <input
                  type="text"
                  value={editing.draft.description}
                  onChange={(event) =>
                    setEditing({
                      ...editing,
                      draft: { ...editing.draft, description: event.target.value },
                    })
                  }
                />
              </Field>
              <Field label="Priority" hint="Lower wins when several commands match.">
                <input
                  type="number"
                  min={0}
                  max={10000}
                  value={editing.draft.priority}
                  onChange={(event) =>
                    setEditing({
                      ...editing,
                      draft: { ...editing.draft, priority: Number(event.target.value) },
                    })
                  }
                />
              </Field>
            </div>

            <div className="grid">
              <Field label="Trigger">
                <select
                  value={editing.draft.trigger_type}
                  onChange={(event) =>
                    setEditing({
                      ...editing,
                      draft: {
                        ...editing.draft,
                        trigger_type: event.target.value as CommandDraft["trigger_type"],
                      },
                    })
                  }
                >
                  <option value="keyword">Keyword</option>
                  <option value="regex">Regular expression</option>
                </select>
              </Field>
              <Field label="Pattern" hint={triggerHelp(editing.draft)}>
                <input
                  type="text"
                  required
                  value={editing.draft.pattern}
                  onChange={(event) =>
                    setEditing({
                      ...editing,
                      draft: { ...editing.draft, pattern: event.target.value },
                    })
                  }
                />
              </Field>
              {editing.draft.trigger_type === "keyword" && (
                <Field
                  label="Match mode"
                  hint={editing.draft.response_type === "script" ? "Scripts always use \"Starts with\", so users can add arguments." : undefined}
                >
                  <select
                    value={editing.draft.match_mode}
                    disabled={editing.draft.response_type === "script"}
                    onChange={(event) =>
                      setEditing({
                        ...editing,
                        draft: {
                          ...editing.draft,
                          match_mode: event.target.value as CommandDraft["match_mode"],
                        },
                      })
                    }
                  >
                    <option value="exact">Exact</option>
                    <option value="prefix">Starts with</option>
                    <option value="contains">Contains</option>
                  </select>
                </Field>
              )}
            </div>

            <div className="row">
              <Checkbox
                label="Enabled"
                checked={editing.draft.enabled}
                onChange={(value) =>
                  setEditing({ ...editing, draft: { ...editing.draft, enabled: value } })
                }
              />
              <Checkbox
                label="Show in help"
                checked={editing.draft.show_in_help}
                onChange={(value) =>
                  setEditing({ ...editing, draft: { ...editing.draft, show_in_help: value } })
                }
              />
              <Checkbox
                label="Case sensitive"
                checked={editing.draft.case_sensitive}
                onChange={(value) =>
                  setEditing({ ...editing, draft: { ...editing.draft, case_sensitive: value } })
                }
              />
            </div>

            <Field label="Response type">
              <select
                value={editing.draft.response_type}
                onChange={(event) =>
                  setEditing({
                    ...editing,
                    draft: {
                      ...editing.draft,
                      response_type: event.target.value as CommandDraft["response_type"],
                      // Scripts take arguments, which only "Starts with" leaves room for.
                      ...(event.target.value === "script" && editing.draft.trigger_type === "keyword"
                        ? { match_mode: "prefix" as const }
                        : {}),
                    },
                  })
                }
              >
                <option value="static">Static text</option>
                <option value="integration">Call an API integration</option>
                <option value="builtin">Built-in behaviour</option>
                <option value="script">Run a script</option>
              </select>
            </Field>

            {editing.draft.response_type === "static" && (
              <Field
                label="Reply template"
                hint="Jinja. Available: {{ user.username }}, {{ user.mention }}, {{ args }}, {{ argv }}, {{ groups }}, {{ room.id }}, {{ now }}."
              >
                <textarea
                  value={editing.draft.static_response}
                  onChange={(event) =>
                    setEditing({
                      ...editing,
                      draft: { ...editing.draft, static_response: event.target.value },
                    })
                  }
                />
              </Field>
            )}

            {editing.draft.response_type === "integration" && (
              <Field
                label="Integration"
                hint="Command arguments fill the integration's parameters in order."
              >
                <select
                  value={editing.draft.integration_id ?? ""}
                  onChange={(event) =>
                    setEditing({
                      ...editing,
                      draft: {
                        ...editing.draft,
                        integration_id: event.target.value ? Number(event.target.value) : null,
                      },
                    })
                  }
                >
                  <option value="">Select an integration…</option>
                  {integrationList.map((integration) => (
                    <option key={integration.id} value={integration.id}>
                      {integration.name}
                      {integration.enabled ? "" : " (disabled)"}
                    </option>
                  ))}
                </select>
              </Field>
            )}

            {editing.draft.response_type === "builtin" && (
              <Field label="Built-in" hint="Behaviour implemented in the bot itself.">
                <select
                  value={editing.draft.builtin_name ?? ""}
                  onChange={(event) =>
                    setEditing({
                      ...editing,
                      draft: { ...editing.draft, builtin_name: event.target.value || null },
                    })
                  }
                >
                  <option value="">Select a built-in…</option>
                  {(builtins.data ?? []).map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              </Field>
            )}

            {editing.draft.response_type === "script" && (
              <div className="grid">
                <Field
                  label="Script"
                  hint={
                    <>
                      Files in <code>{scripts.data?.directory ?? "scripts/"}</code>. Add or edit them
                      on the server, then{" "}
                      <button type="button" className="link" onClick={() => void scripts.reload()}>
                        refresh this list
                      </button>
                      .
                    </>
                  }
                >
                  <select
                    value={editing.draft.script_name ?? ""}
                    onChange={(event) =>
                      setEditing({
                        ...editing,
                        draft: { ...editing.draft, script_name: event.target.value || null },
                      })
                    }
                  >
                    <option value="">Select a script…</option>
                    {editing.draft.script_name &&
                      !(scripts.data?.scripts ?? []).some((s) => s.name === editing.draft.script_name) && (
                        <option value={editing.draft.script_name}>
                          {editing.draft.script_name} (missing)
                        </option>
                      )}
                    {(scripts.data?.scripts ?? []).map((script) => (
                      <option key={script.name} value={script.name}>
                        {script.name} ({script.runner})
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Timeout (seconds)" hint="The script is killed if it runs longer.">
                  <input
                    type="number"
                    min={1}
                    max={300}
                    value={editing.draft.script_timeout_seconds}
                    onChange={(event) =>
                      setEditing({
                        ...editing,
                        draft: { ...editing.draft, script_timeout_seconds: Number(event.target.value) },
                      })
                    }
                  />
                </Field>
                <p className="hint" style={{ gridColumn: "1 / -1", margin: 0 }}>
                  Words after the keyword are passed to the script as argv (<code>disk /var -h</code>{" "}
                  runs it with <code>/var</code> and <code>-h</code>); the message context
                  arrives as JSON on stdin and as <code>BOT_USERNAME</code>, <code>BOT_ARGS</code>,{" "}
                  <code>BOT_ROOM_ID</code>… variables. What it prints becomes the reply; a non-zero
                  exit is reported as an error.
                </p>
              </div>
            )}

            <div className="row">
              <button className="primary" type="submit" disabled={busy}>
                {busy ? "Saving…" : "Save command"}
              </button>
              <button type="button" onClick={() => setEditing(null)}>
                Cancel
              </button>
            </div>
          </form>
        </Card>
      )}

      <Card title="Configured commands">
        {(commands.data ?? []).length === 0 ? (
          <Empty>No commands yet.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Trigger</th>
                  <th>Answers with</th>
                  <th>Priority</th>
                  <th>State</th>
                  {isAdmin && <th />}
                </tr>
              </thead>
              <tbody>
                {(commands.data ?? []).map((command) => (
                  <tr key={command.id}>
                    <td>
                      <strong>{command.name}</strong>
                      {command.description && <div className="muted">{command.description}</div>}
                    </td>
                    <td>
                      <code>
                        {command.pattern}
                        {command.trigger_type === "keyword" && command.match_mode === "prefix" && " …"}
                      </code>
                      <div className="muted">
                        {command.trigger_type}
                        {command.trigger_type === "keyword" && ` · ${command.match_mode}`}
                      </div>
                    </td>
                    <td>
                      {command.response_type === "static" && <span className="muted">static text</span>}
                      {command.response_type === "builtin" && <code>{command.builtin_name}</code>}
                      {command.response_type === "script" && (
                        <>
                          <code>{command.script_name}</code>
                          <div className="muted">script</div>
                        </>
                      )}
                      {command.response_type === "integration" && (
                        <code>
                          {integrationList.find((i) => i.id === command.integration_id)?.name ??
                            `#${command.integration_id}`}
                        </code>
                      )}
                    </td>
                    <td>{command.priority}</td>
                    <td>
                      {command.enabled ? <Pill kind="ok">on</Pill> : <Pill>off</Pill>}
                    </td>
                    {isAdmin && (
                      <td>
                        <div className="row">
                          <button type="button" onClick={() => setEditing({ id: command.id, draft: toDraft(command) })}>
                            Edit
                          </button>
                          <button type="button" onClick={() => toggle(command)}>
                            {command.enabled ? "Disable" : "Enable"}
                          </button>
                          <button className="danger" type="button" onClick={() => remove(command)}>
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

      {isAdmin && (
        <Card title="Try a message">
          <p className="hint" style={{ marginTop: 0 }}>
            Runs the message through the live router. Built-ins, integrations and scripts execute for real.
          </p>
          <div className="row">
            <input
              type="text"
              placeholder="/weather Hanoi"
              value={probe}
              onChange={(event) => setProbe(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  void runProbe();
                }
              }}
              style={{ flex: "1 1 240px" }}
            />
            <button type="button" onClick={runProbe} disabled={!probe.trim()}>
              Run
            </button>
          </div>
          {probeResult && <pre style={{ marginTop: "0.75rem" }}>{probeResult}</pre>}
        </Card>
      )}
    </>
  );
}
