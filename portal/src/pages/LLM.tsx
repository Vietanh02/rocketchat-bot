import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { LLMConfig, LLMConfigUpdate, LLMTestResult } from "../api/types";
import { useAuth } from "../auth";
import { Banner, Card, Checkbox, Field, Loading, Pill } from "../components/ui";
import { errorMessage, useResource } from "../hooks";

/** The form keeps optional numbers as text, so "empty" can mean "gateway default". */
interface Form extends Omit<LLMConfig, "max_tokens" | "temperature"> {
  max_tokens: string;
  temperature: string;
}

const toForm = (config: LLMConfig): Form => ({
  ...config,
  max_tokens: config.max_tokens == null ? "" : String(config.max_tokens),
  temperature: config.temperature == null ? "" : String(config.temperature),
});

const optionalNumber = (text: string) => (text.trim() === "" ? null : Number(text));

export default function LLMPage() {
  const { isAdmin } = useAuth();
  const config = useResource(api.getLLM, []);
  const [form, setForm] = useState<Form | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [notice, setNotice] = useState<{ kind: "ok" | "error" | "warn"; text: string } | null>(null);
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const [prompt, setPrompt] = useState("Xin chào! Bạn là ai?");
  const [testResult, setTestResult] = useState<LLMTestResult | null>(null);

  useEffect(() => {
    if (config.data) setForm(toForm(config.data));
  }, [config.data]);

  if (config.loading && !form) return <Loading />;
  if (config.error) return <Banner kind="error">{config.error}</Banner>;
  if (!form) return null;

  const set = <K extends keyof Form>(key: K, value: Form[K]) => setForm({ ...form, [key]: value });

  /** What the form says, as the API expects it. */
  const values = (): LLMConfigUpdate => ({
    base_url: form.base_url,
    model: form.model,
    system_prompt: form.system_prompt,
    timeout_seconds: form.timeout_seconds,
    max_tokens: optionalNumber(form.max_tokens),
    temperature: optionalNumber(form.temperature),
    format_for_rocketchat: form.format_for_rocketchat,
  });

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy("save");
    setNotice(null);
    const changes: LLMConfigUpdate = {
      ...values(),
      enabled: form!.enabled,
      history_messages: form!.history_messages,
    };
    // The key is only sent when typed, so an untouched field never clears it.
    if (apiKey) changes.api_key = apiKey;
    try {
      const saved = await api.updateLLM(changes);
      config.setData(saved);
      setApiKey("");
      setNotice({
        kind: saved.enabled || !saved.configured ? "ok" : "warn",
        text: saved.enabled
          ? `Saved. Messages no command matches are now answered by ${saved.model}.`
          : saved.configured
            ? "Saved. The model is configured but off; tick “Answer unmatched messages” to use it."
            : "Saved.",
      });
    } catch (caught) {
      setNotice({ kind: "error", text: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  }

  async function test() {
    setBusy("test");
    setTestResult(null);
    try {
      setTestResult(await api.testLLM({ ...values(), api_key: apiKey || undefined, prompt }));
    } catch (caught) {
      setTestResult({ ok: false, message: errorMessage(caught), reply: null, duration_ms: null });
    } finally {
      setBusy(null);
    }
  }

  const clearKey = () => {
    setApiKey("");
    void api
      .updateLLM({ api_key: "" })
      .then((saved) => {
        config.setData(saved);
        setNotice({ kind: "ok", text: "Stored API key cleared." });
      })
      .catch((caught) => setNotice({ kind: "error", text: errorMessage(caught) }));
  };

  const canEnable = form.configured || Boolean(form.base_url && form.model && (apiKey || form.has_api_key));

  return (
    <form onSubmit={save}>
      <div className="page-head">
        <div>
          <h1>Language model</h1>
          <p>
            Answers messages that match no command, using any OpenAI-compatible API. Commands always
            answer first.
          </p>
        </div>
        {isAdmin && (
          <div className="row">
            <button className="primary" type="submit" disabled={busy !== null}>
              {busy === "save" ? "Saving…" : "Save"}
            </button>
          </div>
        )}
      </div>

      {notice && <Banner kind={notice.kind}>{notice.text}</Banner>}
      {!isAdmin && <Banner kind="warn">You have read-only access; changes cannot be saved.</Banner>}

      <Card
        title="Service"
        action={
          config.data?.enabled ? (
            <Pill kind="ok">On</Pill>
          ) : config.data?.configured ? (
            <Pill kind="warn">Off</Pill>
          ) : (
            <Pill>Not configured</Pill>
          )
        }
      >
        <div className="stack">
          <Checkbox
            label="Answer unmatched messages with the language model"
            checked={form.enabled}
            disabled={!isAdmin || (!form.enabled && !canEnable)}
            onChange={(value) => set("enabled", value)}
          />
          <p className="hint" style={{ marginTop: "-0.5rem" }}>
            {canEnable
              ? "DMs and @mentions that match no command go to the model instead of getting the fallback reply."
              : "Set the gateway, API key and model first."}
          </p>

          <Field
            label="API gateway"
            hint="Base URL of an OpenAI-compatible API, up to the version, e.g. https://localcal.ai/aigw/ai/v1. The bot calls …/chat/completions."
          >
            <input
              type="url"
              placeholder="https://api.example.com/v1"
              value={form.base_url}
              disabled={!isAdmin}
              onChange={(event) => set("base_url", event.target.value)}
            />
          </Field>

          <Field
            label="API key"
            hint={
              form.has_api_key ? (
                <>
                  A key is stored (encrypted). Type a new one to replace it
                  {isAdmin && (
                    <>
                      {", or "}
                      <button type="button" className="link" onClick={clearKey}>
                        clear it
                      </button>
                    </>
                  )}
                  .
                </>
              ) : (
                "Sent as a Bearer token. Stored encrypted and never shown again."
              )
            }
          >
            <input
              type="password"
              autoComplete="off"
              placeholder={form.has_api_key ? "•••••••• (stored)" : "sk-…"}
              value={apiKey}
              disabled={!isAdmin}
              onChange={(event) => setApiKey(event.target.value)}
            />
          </Field>

          <div className="grid">
            <Field label="Model" hint="As the gateway names it, e.g. glm-4.6.">
              <input
                type="text"
                value={form.model}
                disabled={!isAdmin}
                onChange={(event) => set("model", event.target.value)}
              />
            </Field>
            <Field
              label="Timeout (seconds)"
              hint="How long to wait for the gateway. When streaming: for the first token, and between tokens."
            >
              <input
                type="number"
                min={1}
                max={600}
                value={form.timeout_seconds}
                disabled={!isAdmin}
                onChange={(event) => set("timeout_seconds", Number(event.target.value))}
              />
            </Field>
          </div>
        </div>
      </Card>

      <Card title="Answers">
        <div className="stack">
          <Field
            label="System prompt"
            hint={
              <>
                <code>{"{bot}"}</code> becomes the bot's username. Leave empty for the built-in prompt.
              </>
            }
          >
            <textarea
              rows={4}
              placeholder={form.default_system_prompt}
              value={form.system_prompt}
              disabled={!isAdmin}
              onChange={(event) => set("system_prompt", event.target.value)}
            />
          </Field>

          <div className="grid">
            <Field label="Max tokens" hint="Empty: the gateway's default.">
              <input
                type="number"
                min={1}
                value={form.max_tokens}
                disabled={!isAdmin}
                onChange={(event) => set("max_tokens", event.target.value)}
              />
            </Field>
            <Field label="Temperature" hint="0 – 2. Empty: the gateway's default.">
              <input
                type="number"
                min={0}
                max={2}
                step={0.1}
                value={form.temperature}
                disabled={!isAdmin}
                onChange={(event) => set("temperature", event.target.value)}
              />
            </Field>
            <Field label="History (messages)" hint="Earlier messages sent with each question. 0: none.">
              <input
                type="number"
                min={0}
                max={100}
                value={form.history_messages}
                disabled={!isAdmin}
                onChange={(event) => set("history_messages", Number(event.target.value))}
              />
            </Field>
          </div>

          <Checkbox
            label="Format answers for Rocket.Chat"
            checked={form.format_for_rocketchat}
            disabled={!isAdmin}
            onChange={(value) => set("format_for_rocketchat", value)}
          />
          <p className="hint" style={{ marginTop: "-0.5rem" }}>
            {form.format_for_rocketchat
              ? "Rewrites the model's Markdown: **bold** → *bold*, *italic* → _italic_, headings → bold, tables → aligned code blocks, <think> removed."
              : "Posted as the model writes it (only <think> is removed). The built-in prompt then asks for Rocket.Chat syntax."}
          </p>
          <p className="hint" style={{ marginTop: 0 }}>
            Streaming follows <Link to="/settings">Bot settings → Reply style</Link>: the answer is typed
            out as the model writes it.
          </p>
        </div>
      </Card>

      {isAdmin && (
        <Card title="Try it">
          <div className="stack">
            <Field label="Question" hint="Uses the values on this page, saved or not.">
              <input type="text" value={prompt} onChange={(event) => setPrompt(event.target.value)} />
            </Field>
            <div className="row">
              <button type="button" onClick={test} disabled={busy !== null || !prompt.trim()}>
                {busy === "test" ? "Waiting for the model…" : "Send test question"}
              </button>
            </div>
            {testResult && (
              <>
                <Banner kind={testResult.ok ? "ok" : "error"}>{testResult.message}</Banner>
                {testResult.reply && (
                  <div>
                    <label>As posted in Rocket.Chat</label>
                    <pre>{testResult.reply}</pre>
                  </div>
                )}
              </>
            )}
          </div>
        </Card>
      )}
    </form>
  );
}
