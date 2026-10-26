import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { BotConfig, BotConfigUpdate, ReplyMode } from "../api/types";
import { useAuth } from "../auth";
import { Banner, Card, Checkbox, Field, Loading } from "../components/ui";
import { browserTimezone } from "../format";
import { errorMessage, useResource } from "../hooks";

const TIMEZONES = (() => {
  const supported = (Intl as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf;
  const zones = supported ? supported("timeZone") : ["UTC"];
  return zones.includes("UTC") ? zones : ["UTC", ...zones];
})();

export default function SettingsPage() {
  const { isAdmin } = useAuth();
  const config = useResource(api.getConfig, []);
  const [form, setForm] = useState<BotConfig | null>(null);
  const [token, setToken] = useState("");
  const [password, setPassword] = useState("");
  const [notice, setNotice] = useState<{ kind: "ok" | "error" | "warn"; text: string } | null>(null);
  const [busy, setBusy] = useState<"save" | "test" | null>(null);

  useEffect(() => {
    if (config.data) setForm(config.data);
  }, [config.data]);

  if (config.loading && !form) return <Loading />;
  if (config.error) return <Banner kind="error">{config.error}</Banner>;
  if (!form) return null;

  const set = <K extends keyof BotConfig>(key: K, value: BotConfig[K]) =>
    setForm({ ...form, [key]: value });

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy("save");
    setNotice(null);
    const changes: BotConfigUpdate = {
      rc_rest_url: form!.rc_rest_url,
      rc_ws_url: form!.rc_ws_url,
      bot_user_id: form!.bot_user_id,
      bot_username: form!.bot_username,
      enabled: form!.enabled,
      greeting: form!.greeting,
      fallback_reply: form!.fallback_reply,
      reply_in_thread: form!.reply_in_thread,
      respond_in_channels: form!.respond_in_channels,
      default_timezone: form!.default_timezone,
      reply_mode: form!.reply_mode,
      stream_chunk_chars: form!.stream_chunk_chars,
      stream_interval_ms: form!.stream_interval_ms,
    };
    // Secrets are only sent when the admin typed something, so an untouched
    // field never clears the stored value.
    if (token) changes.bot_auth_token = token;
    if (password) changes.bot_password = password;

    try {
      const saved = await api.updateConfig(changes);
      config.setData(saved);
      setForm(saved);
      setToken("");
      setPassword("");
      const status = await api.getStatus();
      setNotice(
        status.connected
          ? { kind: "ok", text: `Saved. Connected as @${status.bot_username}.` }
          : { kind: "warn", text: `Saved, but not connected: ${status.last_error ?? "unknown reason"}` },
      );
    } catch (caught) {
      setNotice({ kind: "error", text: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  }

  async function test() {
    setBusy("test");
    setNotice(null);
    try {
      const result = await api.testConnection({
        rc_rest_url: form!.rc_rest_url,
        bot_user_id: form!.bot_user_id ?? undefined,
        bot_username: form!.bot_username ?? undefined,
        bot_auth_token: token || undefined,
        bot_password: password || undefined,
      });
      setNotice({ kind: result.ok ? "ok" : "error", text: result.message });
    } catch (caught) {
      setNotice({ kind: "error", text: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  }

  const clearToken = () => {
    setToken("");
    void api.updateConfig({ bot_auth_token: "" }).then((saved) => {
      config.setData(saved);
      setForm(saved);
      setNotice({ kind: "ok", text: "Stored access token cleared." });
    });
  };

  return (
    <form onSubmit={save}>
      <div className="page-head">
        <div>
          <h1>Bot settings</h1>
          <p>How the bot connects to Rocket.Chat and how it behaves in chat.</p>
        </div>
        {isAdmin && (
          <div className="row">
            <button type="button" onClick={test} disabled={busy !== null}>
              {busy === "test" ? "Testing…" : "Test connection"}
            </button>
            <button className="primary" type="submit" disabled={busy !== null}>
              {busy === "save" ? "Saving…" : "Save & reconnect"}
            </button>
          </div>
        )}
      </div>

      {notice && <Banner kind={notice.kind}>{notice.text}</Banner>}
      {!isAdmin && <Banner kind="warn">You have read-only access; changes cannot be saved.</Banner>}

      <Card title="Rocket.Chat connection">
        <div className="stack">
          <Field label="REST URL" hint="e.g. http://localhost:3000">
            <input
              type="text"
              value={form.rc_rest_url}
              disabled={!isAdmin}
              onChange={(event) => set("rc_rest_url", event.target.value)}
            />
          </Field>
          <Field label="Realtime (websocket) URL" hint="e.g. ws://localhost:3000/websocket">
            <input
              type="text"
              value={form.rc_ws_url}
              disabled={!isAdmin}
              onChange={(event) => set("rc_ws_url", event.target.value)}
            />
          </Field>
          <Field label="Bot username">
            <input
              type="text"
              value={form.bot_username ?? ""}
              disabled={!isAdmin}
              onChange={(event) => set("bot_username", event.target.value)}
            />
          </Field>
          <Field
            label="Personal access token"
            hint={
              form.has_auth_token ? (
                <>
                  A token is stored. Type a new one to replace it
                  {isAdmin && (
                    <>
                      {", or "}
                      <button type="button" className="link" onClick={clearToken}>
                        clear it
                      </button>
                    </>
                  )}
                  .
                </>
              ) : (
                "Preferred over a password. Create one in Rocket.Chat under My Account → Personal Access Tokens."
              )
            }
          >
            <input
              type="password"
              autoComplete="off"
              placeholder={form.has_auth_token ? "•••••••• (stored)" : ""}
              value={token}
              disabled={!isAdmin}
              onChange={(event) => setToken(event.target.value)}
            />
          </Field>
          <Field label="User ID" hint="Required alongside a personal access token.">
            <input
              type="text"
              value={form.bot_user_id ?? ""}
              disabled={!isAdmin}
              onChange={(event) => set("bot_user_id", event.target.value)}
            />
          </Field>
          <Field
            label="Password (fallback)"
            hint={
              form.has_password
                ? "A password is stored. Used only when no access token is set."
                : "Used only when no access token is set."
            }
          >
            <input
              type="password"
              autoComplete="off"
              placeholder={form.has_password ? "•••••••• (stored)" : ""}
              value={password}
              disabled={!isAdmin}
              onChange={(event) => setPassword(event.target.value)}
            />
          </Field>
        </div>
      </Card>

      <Card title="Behaviour">
        <div className="stack">
          <div className="row">
            <Checkbox
              label="Bot enabled"
              checked={form.enabled}
              disabled={!isAdmin}
              onChange={(value) => set("enabled", value)}
            />
            <Checkbox
              label="Answer in channels (when mentioned)"
              checked={form.respond_in_channels}
              disabled={!isAdmin}
              onChange={(value) => set("respond_in_channels", value)}
            />
            <Checkbox
              label="Reply in a thread"
              checked={form.reply_in_thread}
              disabled={!isAdmin}
              onChange={(value) => set("reply_in_thread", value)}
            />
          </div>

          <Field label="Greeting" hint="Shown at the top of the help message.">
            <textarea
              value={form.greeting}
              disabled={!isAdmin}
              onChange={(event) => set("greeting", event.target.value)}
            />
          </Field>

          <Field
            label="Fallback reply"
            hint="Sent when nothing matches. Leave empty to stay silent instead."
          >
            <textarea
              value={form.fallback_reply}
              disabled={!isAdmin}
              onChange={(event) => set("fallback_reply", event.target.value)}
            />
          </Field>

          <Field
            label="Default timezone for chat reminders"
            hint={`Your browser reports ${browserTimezone()}.`}
          >
            <select
              value={form.default_timezone}
              disabled={!isAdmin}
              onChange={(event) => set("default_timezone", event.target.value)}
            >
              {TIMEZONES.map((zone) => (
                <option key={zone} value={zone}>
                  {zone}
                </option>
              ))}
            </select>
          </Field>

          <Field
            label="Reply style"
            hint={
              form.reply_mode === "stream"
                ? "The bot posts the start of its reply, then edits that message until the answer is complete."
                : "The bot posts each reply in one go, once it is complete."
            }
          >
            <select
              value={form.reply_mode}
              disabled={!isAdmin}
              onChange={(event) => set("reply_mode", event.target.value as ReplyMode)}
            >
              <option value="whole">Whole message</option>
              <option value="stream">Streaming (text appears progressively)</option>
            </select>
          </Field>

          {form.reply_mode === "stream" && (
            <div className="grid">
              <Field label="Characters per update" hint="Cut at word boundaries where possible.">
                <input
                  type="number"
                  min={1}
                  max={2000}
                  value={form.stream_chunk_chars}
                  disabled={!isAdmin}
                  onChange={(event) => set("stream_chunk_chars", Number(event.target.value))}
                />
              </Field>
              <Field
                label="Delay between updates (ms)"
                hint="Long replies use bigger chunks, so streaming never takes more than about 8 seconds."
              >
                <input
                  type="number"
                  min={50}
                  max={5000}
                  step={50}
                  value={form.stream_interval_ms}
                  disabled={!isAdmin}
                  onChange={(event) => set("stream_interval_ms", Number(event.target.value))}
                />
              </Field>
            </div>
          )}

          <Field
            label="Responder"
            hint="Rule-based matching. An LLM responder can be added later without changing this data."
          >
            <input type="text" value={form.responder_mode} disabled readOnly />
          </Field>
        </div>
      </Card>
    </form>
  );
}
