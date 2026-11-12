import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { McpCallResult, McpServer, McpServerDraft, McpTool, McpToolChanges } from "../api/types";
import { useAuth } from "../auth";
import { Banner, Card, Checkbox, Empty, Field, Loading, Pill } from "../components/ui";
import { formatRelative } from "../format";
import { errorMessage, useResource } from "../hooks";

type Notice = { kind: "ok" | "error" | "warn"; text: string } | null;

interface Form extends Omit<McpServerDraft, "args" | "headers" | "env"> {
  args: string;
  /** JSON text; empty keeps what is stored. */
  headers: string;
  env: string;
}

const EMPTY: Form = {
  name: "",
  description: "",
  transport: "http",
  url: "",
  command: "",
  args: "",
  timeout_seconds: 30,
  keep_alive_seconds: 300,
  enabled: true,
  headers: "",
  env: "",
};

function parseObject(text: string, what: string): Record<string, string> | undefined {
  if (!text.trim()) return undefined;
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error(`${what} is not valid JSON`);
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${what} must be a JSON object`);
  return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([k, v]) => [k, String(v)]));
}

export default function McpPage() {
  const { isAdmin } = useAuth();
  const servers = useResource(api.listMcpServers, []);
  const [editing, setEditing] = useState<{ id: number | null; form: Form } | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const fail = (caught: unknown) => setNotice({ kind: "error", text: errorMessage(caught) });
  const replace = (server: McpServer) =>
    servers.setData((servers.data ?? []).map((s) => (s.id === server.id ? server : s)));

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!editing) return;
    const { form, id } = editing;
    setBusy("save");
    try {
      const draft: McpServerDraft = {
        ...form,
        args: form.args.split("\n").map((a) => a.trim()).filter(Boolean),
        headers: parseObject(form.headers, "Headers"),
        env: parseObject(form.env, "Environment"),
      };
      const saved = id === null ? await api.createMcpServer(draft) : await api.updateMcpServer(id, draft);
      setEditing(null);
      await servers.reload();
      setNotice({
        kind: "ok",
        text: id === null ? `Added ${saved.name}. Refresh its tools, then turn on the ones the model may use.` : "Saved.",
      });
    } catch (caught) {
      fail(caught);
    } finally {
      setBusy(null);
    }
  }

  const refresh = (server: McpServer) => {
    setBusy(`refresh-${server.id}`);
    void api
      .refreshMcpServer(server.id)
      .then((next) => {
        replace(next);
        setNotice({ kind: "ok", text: `${next.name}: ${next.tools.length} tools. New ones start off.` });
      })
      .catch((caught) => {
        fail(caught);
        void servers.reload();
      })
      .finally(() => setBusy(null));
  };

  const remove = (server: McpServer) =>
    window.confirm(`Remove ${server.name} and its ${server.tools.length} tools?`) &&
    void api
      .deleteMcpServer(server.id)
      .then(() => {
        void servers.reload();
        setNotice({ kind: "ok", text: `Removed ${server.name}.` });
      })
      .catch(fail);

  const edit = (server: McpServer) =>
    setEditing({
      id: server.id,
      form: {
        name: server.name,
        description: server.description,
        transport: server.transport,
        url: server.url,
        command: server.command,
        args: server.args.join("\n"),
        timeout_seconds: server.timeout_seconds,
        keep_alive_seconds: server.keep_alive_seconds,
        enabled: server.enabled,
        headers: "",
        env: "",
      },
    });

  const list = servers.data ?? [];

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>MCP servers</h1>
          <p>
            Tools of MCP servers the language model may call. A server's tools start off: turn on only
            those you are happy for anyone it is offered to to call with arguments of their choosing.
            Who gets a tool that is on is up to the policy (see{" "}
            <Link to="/access">Access → Tools by profile</Link>, where MCP tools are for trusted people
            by default), unless you open it to everyone here.
          </p>
        </div>
        {isAdmin && (
          <div className="row">
            <button type="button" onClick={() => setEditing({ id: null, form: { ...EMPTY } })}>
              Add a server
            </button>
          </div>
        )}
      </div>

      {notice && <Banner kind={notice.kind}>{notice.text}</Banner>}

      {editing && (
        <form onSubmit={save}>
          <Card title={editing.id === null ? "Add an MCP server" : `Change ${editing.form.name}`}>
            <ServerForm
              form={editing.form}
              existing={editing.id === null ? null : list.find((s) => s.id === editing.id) ?? null}
              onChange={(form) => setEditing({ ...editing, form })}
            />
            <div className="row" style={{ marginTop: "1rem" }}>
              <button type="submit" className="primary" disabled={busy !== null}>
                {busy === "save" ? "Saving…" : "Save server"}
              </button>
              <button type="button" onClick={() => setEditing(null)}>
                Cancel
              </button>
            </div>
          </Card>
        </form>
      )}

      {servers.loading && !servers.data ? (
        <Loading />
      ) : servers.error ? (
        <Banner kind="error">{servers.error}</Banner>
      ) : list.length === 0 ? (
        <Card>
          <Empty>No MCP servers yet.</Empty>
        </Card>
      ) : (
        list.map((server) => (
          <Card
            key={server.id}
            title={
              <h2>
                {server.name} <Pill kind={server.enabled ? "ok" : undefined}>{server.enabled ? "on" : "off"}</Pill>{" "}
                {server.connected && <Pill>session open</Pill>}
              </h2>
            }
            action={
              isAdmin && (
                <div className="row">
                  <button type="button" disabled={busy !== null} onClick={() => refresh(server)}>
                    {busy === `refresh-${server.id}` ? "Asking…" : "Refresh tools"}
                  </button>
                  <button type="button" onClick={() => edit(server)}>
                    Edit
                  </button>
                  <button type="button" className="danger" onClick={() => remove(server)}>
                    Remove
                  </button>
                </div>
              )
            }
          >
            <div className="stack">
              <div className="muted">
                {server.transport === "http" ? server.url : `${server.command} ${server.args.join(" ")}`}
                {server.header_names.length > 0 && ` · headers: ${server.header_names.join(", ")}`}
                {server.env_names.length > 0 && ` · env: ${server.env_names.join(", ")}`}
                {server.tools_refreshed_at && ` · tools listed ${formatRelative(server.tools_refreshed_at)}`}
                {server.keep_alive_seconds > 0
                  ? ` · session kept ${server.keep_alive_seconds}s idle`
                  : " · a new session per call"}
              </div>
              {server.description && <p style={{ margin: 0 }}>{server.description}</p>}
              {server.last_error && <Banner kind="error">{server.last_error}</Banner>}
              {server.tools.length === 0 ? (
                <Empty>No tools listed yet: use Refresh tools.</Empty>
              ) : (
                <Tools server={server} isAdmin={isAdmin} onChanged={replace} onError={fail} />
              )}
            </div>
          </Card>
        ))
      )}
    </div>
  );
}

function ServerForm({
  form,
  existing,
  onChange,
}: {
  form: Form;
  existing: McpServer | null;
  onChange: (form: Form) => void;
}) {
  const set = <K extends keyof Form>(key: K, value: Form[K]) => onChange({ ...form, [key]: value });
  const keep = (names: string[] | undefined) =>
    names && names.length > 0 ? `Stored: ${names.join(", ")}. Empty keeps them; {} clears them.` : "Empty: none.";
  return (
    <div className="stack">
      <div className="grid">
        <Field label="Name" hint="Letters, digits, - and _. Tools are offered as mcp__<name>__<tool>.">
          <input type="text" required value={form.name} onChange={(e) => set("name", e.target.value)} />
        </Field>
        <Field label="Transport">
          <select value={form.transport} onChange={(e) => set("transport", e.target.value as Form["transport"])}>
            <option value="http">HTTP (Streamable HTTP)</option>
            <option value="stdio">Command on this machine (stdio)</option>
          </select>
        </Field>
        <Field label="Timeout (seconds)">
          <input
            type="number"
            min={1}
            max={300}
            value={form.timeout_seconds}
            onChange={(e) => set("timeout_seconds", Number(e.target.value))}
          />
        </Field>
        <Field
          label="Keep alive (seconds)"
          hint="An idle session (a stdio server's process) is kept this long for the next call. 0: a new one per call."
        >
          <input
            type="number"
            min={0}
            max={86400}
            value={form.keep_alive_seconds}
            onChange={(e) => set("keep_alive_seconds", Number(e.target.value))}
          />
        </Field>
      </div>
      <Field label="Description" hint="For you; the model sees each tool's own description.">
        <input type="text" value={form.description} onChange={(e) => set("description", e.target.value)} />
      </Field>
      {form.transport === "http" ? (
        <>
          <Field
            label="URL"
            hint="e.g. https://mcp.example.com/mcp. A server on this machine: http://host.docker.internal:<port>/mcp."
          >
            <input type="url" value={form.url} onChange={(e) => set("url", e.target.value)} />
          </Field>
          <Field label="Headers (JSON)" hint={`e.g. {"Authorization": "Bearer …"}. Stored encrypted. ${keep(existing?.header_names)}`}>
            <textarea rows={2} value={form.headers} onChange={(e) => set("headers", e.target.value)} spellCheck={false} />
          </Field>
        </>
      ) : (
        <>
          <Field
            label="Command"
            hint="Run in the backend container, in the mcp_servers folder. python is the bot's own Python, with the mcp SDK. The image has no node/npx."
          >
            <input
              type="text"
              value={form.command}
              placeholder="python"
              onChange={(e) => set("command", e.target.value)}
            />
          </Field>
          <Field label="Arguments" hint="One per line, e.g. _example/server.py for mcp_servers/_example/server.py.">
            <textarea rows={3} value={form.args} onChange={(e) => set("args", e.target.value)} spellCheck={false} />
          </Field>
          <Field label="Environment (JSON)" hint={`Stored encrypted. ${keep(existing?.env_names)}`}>
            <textarea rows={2} value={form.env} onChange={(e) => set("env", e.target.value)} spellCheck={false} />
          </Field>
        </>
      )}
      <Checkbox label="On" checked={form.enabled} onChange={(value) => set("enabled", value)} />
    </div>
  );
}

function Tools({
  server,
  isAdmin,
  onChanged,
  onError,
}: {
  server: McpServer;
  isAdmin: boolean;
  onChanged: (server: McpServer) => void;
  onError: (caught: unknown) => void;
}) {
  const [trying, setTrying] = useState<McpTool | null>(null);
  const [args, setArgs] = useState("{}");
  const [result, setResult] = useState<McpCallResult | null>(null);

  const change = (tool: McpTool, changes: McpToolChanges) =>
    void api.setMcpTool(server.id, tool.id, changes).then(onChanged).catch(onError);

  const openToEveryone = (tool: McpTool, everyone: boolean) => {
    if (everyone && !tool.read_only) {
      const what = tool.destructive ? "says it may destroy data" : "does not say it is read-only";
      if (!window.confirm(`${server.name} ${what} for ${tool.name}. Offer it to everyone anyway?`)) return;
    }
    change(tool, { everyone });
  };

  async function run(event: React.FormEvent) {
    event.preventDefault();
    if (!trying) return;
    setResult(null);
    try {
      const parsed = args.trim() ? JSON.parse(args) : {};
      setResult(await api.callMcpTool(server.id, trying.id, parsed));
    } catch (caught) {
      onError(caught);
    }
  }

  return (
    <div className="stack">
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Tool</th>
              <th>Description</th>
              <th>Offered</th>
              <th title="The policy decides, or everyone gets it">To</th>
              <th title="Answers reused for the same arguments, in seconds">Reuse (s)</th>
              {isAdmin && <th />}
            </tr>
          </thead>
          <tbody>
            {server.tools.map((tool) => (
              <tr key={tool.id}>
                <td>
                  <code>{tool.name}</code>{" "}
                  {tool.read_only && <Pill kind="ok">read-only</Pill>}
                  {tool.destructive && <Pill kind="warn">destructive</Pill>}
                  <div className="hint">{tool.tool_name}</div>
                </td>
                <td className="muted">{tool.description || "—"}</td>
                <td>
                  <Checkbox
                    label={tool.enabled ? "on" : "off"}
                    checked={tool.enabled}
                    disabled={!isAdmin}
                    onChange={() => change(tool, { enabled: !tool.enabled })}
                  />
                </td>
                <td>
                  <select
                    value={tool.everyone ? "everyone" : "policy"}
                    disabled={!isAdmin}
                    onChange={(e) => openToEveryone(tool, e.target.value === "everyone")}
                  >
                    <option value="policy">Policy</option>
                    <option value="everyone">Everyone</option>
                  </select>
                </td>
                <td>
                  <input
                    type="number"
                    min={0}
                    max={86400}
                    style={{ width: "6em" }}
                    defaultValue={tool.cache_seconds}
                    key={`${tool.id}-${tool.cache_seconds}`}
                    disabled={!isAdmin}
                    title={tool.read_only ? "" : "Only for a tool whose answer does not depend on when it is called"}
                    onBlur={(e) => {
                      const value = Math.max(0, Math.round(Number(e.target.value) || 0));
                      if (value !== tool.cache_seconds) change(tool, { cache_seconds: value });
                    }}
                  />
                </td>
                {isAdmin && (
                  <td>
                    <button
                      type="button"
                      onClick={() => {
                        setTrying(tool);
                        setResult(null);
                        setArgs("{}");
                      }}
                    >
                      Try
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {trying && (
        <form onSubmit={run} className="stack">
          <Field
            label={`Try ${trying.name}: arguments (JSON)`}
            hint={`Schema: ${JSON.stringify(trying.input_schema.properties ?? {})}`}
          >
            <textarea rows={3} value={args} onChange={(e) => setArgs(e.target.value)} spellCheck={false} />
          </Field>
          <div className="row">
            <button type="submit">Call</button>
            <button type="button" onClick={() => setTrying(null)}>
              Close
            </button>
          </div>
          {result && (
            <div>
              <Banner kind={result.ok ? "ok" : "error"}>
                {result.ok ? "Answered" : "Failed"} in {result.duration_ms} ms
              </Banner>
              <pre>{result.text}</pre>
            </div>
          )}
        </form>
      )}
    </div>
  );
}
