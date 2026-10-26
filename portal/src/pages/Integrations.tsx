import { useState } from "react";
import { api } from "../api/client";
import type { Integration, IntegrationDraft, IntegrationParam, IntegrationTestResult } from "../api/types";
import { useAuth } from "../auth";
import { Banner, Card, Checkbox, Empty, Field, Loading, Pill } from "../components/ui";
import { errorMessage, useResource } from "../hooks";

const BLANK: IntegrationDraft = {
  name: "",
  description: "",
  base_url: "",
  method: "GET",
  path_template: "",
  query_template: "",
  headers_template: "",
  body_template: "",
  reply_template: "",
  auth_type: "none",
  auth_username: null,
  auth_header_name: null,
  timeout_seconds: 10,
  enabled: true,
  params: [],
};

const METHODS: IntegrationDraft["method"][] = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"];

function toDraft(integration: Integration): IntegrationDraft {
  const { id: _i, created_at: _c, updated_at: _u, has_secret: _h, ...draft } = integration;
  return { ...draft, params: draft.params.map((p) => ({ ...p })) };
}

export default function IntegrationsPage() {
  const { isAdmin } = useAuth();
  const integrations = useResource(api.listIntegrations, []);

  const [editing, setEditing] = useState<{ id: number | null; draft: IntegrationDraft } | null>(null);
  const [secret, setSecret] = useState("");
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const [testing, setTesting] = useState<Integration | null>(null);
  const [testArgs, setTestArgs] = useState<Record<string, string>>({});
  const [testResult, setTestResult] = useState<IntegrationTestResult | null>(null);

  function startEdit(integration: Integration | null) {
    setSecret("");
    setEditing(
      integration === null
        ? { id: null, draft: { ...BLANK, params: [] } }
        : { id: integration.id, draft: toDraft(integration) },
    );
  }

  function patchDraft(changes: Partial<IntegrationDraft>) {
    if (!editing) return;
    setEditing({ ...editing, draft: { ...editing.draft, ...changes } });
  }

  function patchParam(index: number, changes: Partial<IntegrationParam>) {
    if (!editing) return;
    const params = editing.draft.params.map((param, i) =>
      i === index ? { ...param, ...changes } : param,
    );
    patchDraft({ params });
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!editing) return;
    setBusy(true);
    setNotice(null);
    const payload: IntegrationDraft = { ...editing.draft };
    if (secret) payload.auth_secret = secret;

    try {
      if (editing.id === null) await api.createIntegration(payload);
      else await api.updateIntegration(editing.id, payload);
      await integrations.reload();
      setEditing(null);
      setSecret("");
      setNotice({ kind: "ok", text: "Integration saved." });
    } catch (caught) {
      setNotice({ kind: "error", text: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }

  async function remove(integration: Integration) {
    if (!window.confirm(`Delete the integration "${integration.name}"?`)) return;
    try {
      await api.deleteIntegration(integration.id);
      await integrations.reload();
    } catch (caught) {
      setNotice({ kind: "error", text: errorMessage(caught) });
    }
  }

  function startTest(integration: Integration) {
    setTesting(integration);
    setTestResult(null);
    setTestArgs(
      Object.fromEntries(integration.params.map((param) => [param.name, param.default_value])),
    );
  }

  async function runTest() {
    if (!testing) return;
    setTestResult(null);
    try {
      setTestResult(await api.testIntegration(testing.id, testArgs));
    } catch (caught) {
      setTestResult({
        ok: false, status_code: null, url: null, reply: null, raw: null,
        error: errorMessage(caught), duration_ms: null,
      });
    }
  }

  if (integrations.loading && !integrations.data) return <Loading />;
  if (integrations.error) return <Banner kind="error">{integrations.error}</Banner>;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>API integrations</h1>
          <p>Outbound HTTP calls the bot can make. Bind one to a command to expose it in chat.</p>
        </div>
        {isAdmin && (
          <button className="primary" type="button" onClick={() => startEdit(null)}>
            New integration
          </button>
        )}
      </div>

      {notice && <Banner kind={notice.kind}>{notice.text}</Banner>}

      {editing && (
        <Card title={editing.id === null ? "New integration" : `Editing "${editing.draft.name}"`}>
          <form className="stack" onSubmit={save}>
            <div className="grid">
              <Field label="Name" hint="Letters, digits, dot, dash, underscore.">
                <input
                  type="text"
                  required
                  value={editing.draft.name}
                  onChange={(event) => patchDraft({ name: event.target.value })}
                />
              </Field>
              <Field label="Description">
                <input
                  type="text"
                  value={editing.draft.description}
                  onChange={(event) => patchDraft({ description: event.target.value })}
                />
              </Field>
              <Field label="Timeout (seconds)">
                <input
                  type="number"
                  min={1}
                  max={120}
                  step={0.5}
                  value={editing.draft.timeout_seconds}
                  onChange={(event) => patchDraft({ timeout_seconds: Number(event.target.value) })}
                />
              </Field>
            </div>

            <div className="grid">
              <Field label="Method">
                <select
                  value={editing.draft.method}
                  onChange={(event) =>
                    patchDraft({ method: event.target.value as IntegrationDraft["method"] })
                  }
                >
                  {METHODS.map((method) => (
                    <option key={method} value={method}>
                      {method}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Base URL" hint="https://api.example.com">
                <input
                  type="text"
                  required
                  value={editing.draft.base_url}
                  onChange={(event) => patchDraft({ base_url: event.target.value })}
                />
              </Field>
              <Field label="Path template" hint="/v1/things/{{ params.id }}">
                <input
                  type="text"
                  value={editing.draft.path_template}
                  onChange={(event) => patchDraft({ path_template: event.target.value })}
                />
              </Field>
            </div>

            <Field label="Query string (JSON object)" hint={'{"q": "{{ params.term }}", "limit": "5"}'}>
              <textarea
                value={editing.draft.query_template}
                onChange={(event) => patchDraft({ query_template: event.target.value })}
              />
            </Field>

            <Field label="Extra headers (JSON object)" hint={'{"Accept": "application/json"}'}>
              <textarea
                value={editing.draft.headers_template}
                onChange={(event) => patchDraft({ headers_template: event.target.value })}
              />
            </Field>

            {editing.draft.method !== "GET" && editing.draft.method !== "HEAD" && (
              <Field label="Request body" hint="JSON is sent as JSON; anything else as plain text.">
                <textarea
                  value={editing.draft.body_template}
                  onChange={(event) => patchDraft({ body_template: event.target.value })}
                />
              </Field>
            )}

            <Field
              label="Reply template"
              hint="Jinja over the response: {{ response.field }}, {{ status }}, {{ body }}. Leave empty to post the raw payload."
            >
              <textarea
                value={editing.draft.reply_template}
                onChange={(event) => patchDraft({ reply_template: event.target.value })}
              />
            </Field>

            <div className="grid">
              <Field label="Authentication">
                <select
                  value={editing.draft.auth_type}
                  onChange={(event) =>
                    patchDraft({ auth_type: event.target.value as IntegrationDraft["auth_type"] })
                  }
                >
                  <option value="none">None</option>
                  <option value="bearer">Bearer token</option>
                  <option value="api_key">API key header</option>
                  <option value="basic">Basic auth</option>
                </select>
              </Field>

              {editing.draft.auth_type === "api_key" && (
                <Field label="Header name" hint="Defaults to X-API-Key.">
                  <input
                    type="text"
                    value={editing.draft.auth_header_name ?? ""}
                    onChange={(event) => patchDraft({ auth_header_name: event.target.value })}
                  />
                </Field>
              )}

              {editing.draft.auth_type === "basic" && (
                <Field label="Username">
                  <input
                    type="text"
                    value={editing.draft.auth_username ?? ""}
                    onChange={(event) => patchDraft({ auth_username: event.target.value })}
                  />
                </Field>
              )}

              {editing.draft.auth_type !== "none" && (
                <Field
                  label="Secret"
                  hint={
                    editing.id !== null &&
                    integrations.data?.find((i) => i.id === editing.id)?.has_secret
                      ? "A secret is stored. Type a new one to replace it."
                      : "Stored encrypted; never shown again."
                  }
                >
                  <input
                    type="password"
                    autoComplete="off"
                    value={secret}
                    onChange={(event) => setSecret(event.target.value)}
                  />
                </Field>
              )}
            </div>

            <div>
              <div className="row spread">
                <label style={{ marginBottom: 0 }}>Parameters</label>
                <button
                  type="button"
                  onClick={() =>
                    patchDraft({
                      params: [
                        ...editing.draft.params,
                        { name: "", description: "", required: false, default_value: "", greedy: false },
                      ],
                    })
                  }
                >
                  Add parameter
                </button>
              </div>
              <p className="hint">
                Command arguments bind to these in order. Reference them as{" "}
                <code>{"{{ params.name }}"}</code>. Only the last may be greedy (it swallows the
                remaining words).
              </p>

              {editing.draft.params.length === 0 ? (
                <p className="muted">No parameters.</p>
              ) : (
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Name</th>
                        <th>Description</th>
                        <th>Default</th>
                        <th>Required</th>
                        <th>Greedy</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {editing.draft.params.map((param, index) => (
                        <tr key={index}>
                          <td>
                            <input
                              type="text"
                              required
                              value={param.name}
                              onChange={(event) => patchParam(index, { name: event.target.value })}
                            />
                          </td>
                          <td>
                            <input
                              type="text"
                              value={param.description}
                              onChange={(event) =>
                                patchParam(index, { description: event.target.value })
                              }
                            />
                          </td>
                          <td>
                            <input
                              type="text"
                              value={param.default_value}
                              onChange={(event) =>
                                patchParam(index, { default_value: event.target.value })
                              }
                            />
                          </td>
                          <td>
                            <input
                              type="checkbox"
                              checked={param.required}
                              onChange={(event) =>
                                patchParam(index, { required: event.target.checked })
                              }
                            />
                          </td>
                          <td>
                            <input
                              type="checkbox"
                              checked={param.greedy}
                              onChange={(event) =>
                                patchParam(index, { greedy: event.target.checked })
                              }
                            />
                          </td>
                          <td>
                            <button
                              className="danger"
                              type="button"
                              onClick={() =>
                                patchDraft({
                                  params: editing.draft.params.filter((_, i) => i !== index),
                                })
                              }
                            >
                              Remove
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            <div className="row">
              <Checkbox
                label="Enabled"
                checked={editing.draft.enabled}
                onChange={(value) => patchDraft({ enabled: value })}
              />
            </div>

            <div className="row">
              <button className="primary" type="submit" disabled={busy}>
                {busy ? "Saving…" : "Save integration"}
              </button>
              <button type="button" onClick={() => setEditing(null)}>
                Cancel
              </button>
            </div>
          </form>
        </Card>
      )}

      {testing && (
        <Card title={`Test "${testing.name}"`}>
          <p className="hint" style={{ marginTop: 0 }}>
            Calls the API for real, with strict templates so missing variables are reported.
          </p>
          <div className="stack">
            {testing.params.length === 0 ? (
              <p className="muted">This integration takes no parameters.</p>
            ) : (
              <div className="grid">
                {testing.params.map((param) => (
                  <Field
                    key={param.name}
                    label={param.name + (param.required ? " *" : "")}
                    hint={param.description || undefined}
                  >
                    <input
                      type="text"
                      value={testArgs[param.name] ?? ""}
                      onChange={(event) =>
                        setTestArgs({ ...testArgs, [param.name]: event.target.value })
                      }
                    />
                  </Field>
                ))}
              </div>
            )}

            <div className="row">
              <button className="primary" type="button" onClick={runTest}>
                Send request
              </button>
              <button type="button" onClick={() => setTesting(null)}>
                Close
              </button>
            </div>

            {testResult && (
              <>
                <Banner kind={testResult.ok ? "ok" : "error"}>
                  {testResult.ok
                    ? `HTTP ${testResult.status_code} in ${testResult.duration_ms}ms`
                    : (testResult.error ?? "Request failed")}
                </Banner>
                {testResult.url && (
                  <p className="hint" style={{ marginTop: 0 }}>
                    <code>{testResult.url}</code>
                  </p>
                )}
                {testResult.reply && (
                  <div>
                    <label>Chat reply</label>
                    <pre>{testResult.reply}</pre>
                  </div>
                )}
                {testResult.raw != null && (
                  <details>
                    <summary className="muted">Raw response</summary>
                    <pre>{JSON.stringify(testResult.raw, null, 2)}</pre>
                  </details>
                )}
              </>
            )}
          </div>
        </Card>
      )}

      <Card title="Configured integrations">
        {(integrations.data ?? []).length === 0 ? (
          <Empty>
            No integrations yet. Add one to let the bot answer from an external API.
          </Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Request</th>
                  <th>Parameters</th>
                  <th>Auth</th>
                  <th>State</th>
                  {isAdmin && <th />}
                </tr>
              </thead>
              <tbody>
                {(integrations.data ?? []).map((integration) => (
                  <tr key={integration.id}>
                    <td>
                      <strong>{integration.name}</strong>
                      {integration.description && (
                        <div className="muted">{integration.description}</div>
                      )}
                    </td>
                    <td>
                      <code>
                        {integration.method} {integration.base_url}
                        {integration.path_template}
                      </code>
                    </td>
                    <td className="muted">
                      {integration.params.length === 0
                        ? "—"
                        : integration.params
                            .map((param) => (param.required ? `<${param.name}>` : `[${param.name}]`))
                            .join(" ")}
                    </td>
                    <td>
                      <span className="muted">{integration.auth_type}</span>
                      {integration.has_secret && (
                        <>
                          {" "}
                          <Pill kind="ok">secret set</Pill>
                        </>
                      )}
                    </td>
                    <td>{integration.enabled ? <Pill kind="ok">on</Pill> : <Pill>off</Pill>}</td>
                    {isAdmin && (
                      <td>
                        <div className="row">
                          <button type="button" onClick={() => startEdit(integration)}>
                            Edit
                          </button>
                          <button type="button" onClick={() => startTest(integration)}>
                            Test
                          </button>
                          <button className="danger" type="button" onClick={() => remove(integration)}>
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
