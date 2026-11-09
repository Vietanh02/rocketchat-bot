import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { ToolPolicy } from "../api/types";
import { errorMessage, useResource } from "../hooks";
import { Banner, Card, Field, Loading, Pill } from "./ui";

/** Which tools the model may use for whom: the safe and trusted profiles. */
export default function ToolPolicyCard({ isAdmin }: { isAdmin: boolean }) {
  const policy = useResource(api.getPolicy, []);
  const tools = useResource(api.policyTools, []);
  const [form, setForm] = useState<ToolPolicy | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (policy.data) setForm(policy.data);
  }, [policy.data]);

  if (policy.error) return <Banner kind="error">{policy.error}</Banner>;
  if (!form) return <Loading />;

  const set = (key: keyof ToolPolicy, value: string) => setForm({ ...form, [key]: value });

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setNotice(null);
    try {
      policy.setData(await api.updatePolicy(form!));
      void tools.reload();
      setNotice({ kind: "ok", text: "Saved." });
    } catch (caught) {
      setNotice({ kind: "error", text: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }

  const globsField = (key: keyof ToolPolicy, label: string, hint: string) => (
    <Field label={label} hint={hint}>
      <textarea
        rows={3}
        value={form[key]}
        disabled={!isAdmin}
        onChange={(event) => set(key, event.target.value)}
        spellCheck={false}
      />
    </Field>
  );

  return (
    <form onSubmit={save}>
      <Card title="Tools by profile">
        <div className="stack">
          <p className="hint" style={{ marginTop: 0 }}>
            Which tools the language model may use depends on who is asking. The people listed as
            trusted get the <em>trusted</em> profile; everyone else gets <em>safe</em>. A tool is
            offered when its name matches an allow pattern and no deny pattern, one per line:{" "}
            <code>*</code> any, <code>mcp__*</code> every MCP tool, <code>mcp__github__*</code> one
            server's, <code>skill_run</code> running skill scripts. In a channel the profile is the
            sender's; a job uses its starter's.
          </p>
          {notice && <Banner kind={notice.kind}>{notice.text}</Banner>}
          <Field label="Trusted people" hint="Rocket.Chat usernames, one per line.">
            <textarea
              rows={3}
              value={form.trusted_usernames}
              disabled={!isAdmin}
              onChange={(event) => set("trusted_usernames", event.target.value)}
              spellCheck={false}
            />
          </Field>
          <div className="grid">
            {globsField("safe_allow", "Safe: allow", "Empty: no tools at all.")}
            {globsField("safe_deny", "Safe: deny", "Default: MCP tools and skill scripts.")}
            {globsField("trusted_allow", "Trusted: allow", "")}
            {globsField("trusted_deny", "Trusted: deny", "")}
          </div>
          {isAdmin && (
            <div className="row">
              <button type="submit" className="primary" disabled={busy}>
                {busy ? "Saving…" : "Save tool profiles"}
              </button>
            </div>
          )}
          {tools.data && tools.data.length > 0 && (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Tool (saved settings)</th>
                    <th>Kind</th>
                    <th>Safe</th>
                    <th>Trusted</th>
                  </tr>
                </thead>
                <tbody>
                  {tools.data.map((tool) => (
                    <tr key={tool.name}>
                      <td>
                        <code>{tool.name}</code>
                      </td>
                      <td className="muted">{tool.kind}</td>
                      <td>{tool.safe ? <Pill kind="ok">yes</Pill> : <Pill>no</Pill>}</td>
                      <td>{tool.trusted ? <Pill kind="ok">yes</Pill> : <Pill>no</Pill>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </Card>
    </form>
  );
}
