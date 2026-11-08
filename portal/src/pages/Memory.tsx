import { useEffect, useState } from "react";
import { api } from "../api/client";
import type {
  EmbeddingConfig,
  EmbeddingConfigUpdate,
  EmbeddingTestResult,
  MemoryConfig,
  MemoryNote,
  MemoryScope,
  Profile,
  ProfileUpdate,
} from "../api/types";
import { useAuth } from "../auth";
import { Banner, Card, Checkbox, Empty, Field, Loading, Pill, Stat } from "../components/ui";
import { formatDateTime } from "../format";
import { errorMessage, useResource } from "../hooks";

type Notice = { kind: "ok" | "error" | "warn"; text: string } | null;

const PAGE_SIZE = 50;

const SOURCE_LABEL: Record<string, string> = {
  tool: "model",
  auto: "extracted",
  portal: "portal",
};

/** Empty means "the model's own size"; the form keeps it as text. */
interface EmbeddingForm extends Omit<EmbeddingConfig, "dimensions"> {
  dimensions: string;
}

const toForm = (config: EmbeddingConfig): EmbeddingForm => ({
  ...config,
  dimensions: config.dimensions == null ? "" : String(config.dimensions),
});

export default function MemoryPage() {
  const { isAdmin } = useAuth();
  const status = useResource(api.memoryStatus, []);
  const [notice, setNotice] = useState<Notice>(null);

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Memory</h1>
          <p>
            What the assistant remembers between conversations: notes, kept per person (their DMs),
            per channel, or for everyone, and a profile of each person it talks to.
          </p>
        </div>
      </div>

      {notice && <Banner kind={notice.kind}>{notice.text}</Banner>}
      {!isAdmin && (
        <Banner kind="warn">
          You have read-only access. Notes and profiles hold what people told the bot, so only admins
          see them.
        </Banner>
      )}

      <Card
        title="State"
        action={
          isAdmin &&
          status.data?.semantic &&
          status.data.pending > 0 && (
            <button
              type="button"
              onClick={() =>
                void api
                  .reindexMemory()
                  .then((next) => {
                    status.setData(next);
                    setNotice({ kind: "ok", text: "Every note now has a vector of the current model." });
                  })
                  .catch((caught) => setNotice({ kind: "error", text: errorMessage(caught) }))
              }
            >
              Embed {status.data.pending} waiting note{status.data.pending === 1 ? "" : "s"}
            </button>
          )
        }
      >
        {status.loading && !status.data ? (
          <Loading />
        ) : status.error ? (
          <Banner kind="error">{status.error}</Banner>
        ) : status.data ? (
          <div className="stack">
            <div className="grid">
              <Stat label="Notes" value={status.data.notes} />
              <Stat
                label="Waiting for a vector"
                value={status.data.embedding_configured ? status.data.pending : "—"}
              />
              <Stat label="Vectors in Qdrant" value={status.data.vector_db.memory_vectors ?? "—"} />
            </div>
            <div className="row">
              <span>
                Vector database:{" "}
                {!status.data.vector_db.configured ? (
                  <Pill>not set up</Pill>
                ) : status.data.vector_db.reachable ? (
                  <Pill kind="ok">Qdrant {status.data.vector_db.version}</Pill>
                ) : (
                  <Pill kind="error">unreachable</Pill>
                )}
              </span>
              <span>
                Embedding model:{" "}
                {status.data.embedding_configured ? <Pill kind="ok">set up</Pill> : <Pill>not set up</Pill>}
              </span>
              <span>
                Search:{" "}
                {status.data.semantic ? (
                  <Pill kind="ok">by meaning and words</Pill>
                ) : (
                  <Pill kind="warn">by words only</Pill>
                )}
              </span>
            </div>
            {status.data.vector_db.error && <p className="hint">{status.data.vector_db.error}</p>}
            {!status.data.vector_db.configured && (
              <p className="hint">
                Set <code>APP_QDRANT_URL</code> for the backend (docker compose sets it to the bundled
                Qdrant) to also find notes that say the same thing in other words.
              </p>
            )}
          </div>
        ) : null}
      </Card>

      <BehaviourCard isAdmin={isAdmin} onNotice={setNotice} />
      <EmbeddingCard isAdmin={isAdmin} onNotice={setNotice} onSaved={() => void status.reload()} />
      {isAdmin && <NotesCard onNotice={setNotice} onChanged={() => void status.reload()} />}
      {isAdmin && <ProfilesCard onNotice={setNotice} />}
    </div>
  );
}

// ------------------------------------------------------------------------- //

function BehaviourCard({ isAdmin, onNotice }: { isAdmin: boolean; onNotice: (n: Notice) => void }) {
  const config = useResource(api.getMemoryConfig, []);
  const [form, setForm] = useState<MemoryConfig | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (config.data) setForm(config.data);
  }, [config.data]);

  if (!form) return <Card title="Behaviour">{config.error ? <Banner kind="error">{config.error}</Banner> : <Loading />}</Card>;
  const set = <K extends keyof MemoryConfig>(key: K, value: MemoryConfig[K]) => setForm({ ...form, [key]: value });

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      config.setData(await api.updateMemoryConfig(form!));
      onNotice({ kind: "ok", text: "Memory settings saved. They apply to the next message." });
    } catch (caught) {
      onNotice({ kind: "error", text: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Behaviour" action={form.enabled ? <Pill kind="ok">On</Pill> : <Pill>Off</Pill>}>
      <form className="stack" onSubmit={save}>
        <Checkbox
          label="Remember things between conversations"
          checked={form.enabled}
          disabled={!isAdmin}
          onChange={(value) => set("enabled", value)}
        />
        <p className="hint" style={{ marginTop: "-0.5rem" }}>
          The model gets tools to save, search and forget notes, relevant notes are shown with each
          question, and in DMs it sees and can update the person's profile. Off: none of this, and no
          tools are sent unless integrations offer some.
        </p>
        <Checkbox
          label="Pick facts to remember after each answer"
          checked={form.auto_extract}
          disabled={!isAdmin || !form.enabled}
          onChange={(value) => set("auto_extract", value)}
        />
        <p className="hint" style={{ marginTop: "-0.5rem" }}>
          One more, small model call per answer, with the fast model if the Assistant page sets one.
          Without it, notes are only saved when the model decides to, or when asked to remember.
        </p>
        <div className="grid">
          <Field
            label="Notes shown with a question"
            hint="The most relevant ones. When a conversation can see no more notes than this, all are shown. 0: none."
          >
            <input
              type="number"
              min={0}
              max={30}
              value={form.prompt_notes}
              disabled={!isAdmin}
              onChange={(event) => set("prompt_notes", Number(event.target.value))}
            />
          </Field>
          <Field
            label="Minimum similarity"
            hint="0 – 1. A note found only by meaning must be at least this close to the question."
          >
            <input
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={form.min_score}
              disabled={!isAdmin}
              onChange={(event) => set("min_score", Number(event.target.value))}
            />
          </Field>
        </div>
        {isAdmin && (
          <div className="row">
            <button className="primary" type="submit" disabled={busy}>
              Save
            </button>
          </div>
        )}
      </form>
    </Card>
  );
}

// ------------------------------------------------------------------------- //

function EmbeddingCard({
  isAdmin,
  onNotice,
  onSaved,
}: {
  isAdmin: boolean;
  onNotice: (n: Notice) => void;
  onSaved: () => void;
}) {
  const config = useResource(api.getEmbedding, []);
  const [form, setForm] = useState<EmbeddingForm | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [rerankKey, setRerankKey] = useState("");
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const [result, setResult] = useState<EmbeddingTestResult | null>(null);

  useEffect(() => {
    if (config.data) setForm(toForm(config.data));
  }, [config.data]);

  if (!form)
    return <Card title="Embedding model">{config.error ? <Banner kind="error">{config.error}</Banner> : <Loading />}</Card>;
  const set = <K extends keyof EmbeddingForm>(key: K, value: EmbeddingForm[K]) => setForm({ ...form, [key]: value });

  const values = (): EmbeddingConfigUpdate => ({
    provider: form.provider,
    base_url: form.base_url,
    model: form.model,
    dimensions: form.dimensions.trim() === "" ? null : Number(form.dimensions),
    query_prefix: form.query_prefix,
    document_prefix: form.document_prefix,
    timeout_seconds: form.timeout_seconds,
    rerank_provider: form.rerank_provider,
    rerank_base_url: form.rerank_base_url,
    rerank_model: form.rerank_model,
  });

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy("save");
    const changes = values();
    // Keys are only sent when typed, so an untouched field never clears them.
    if (apiKey) changes.api_key = apiKey;
    if (rerankKey) changes.rerank_api_key = rerankKey;
    try {
      const saved = await api.updateEmbedding(changes);
      config.setData(saved);
      setApiKey("");
      setRerankKey("");
      onSaved();
      onNotice({
        kind: "ok",
        text: saved.configured
          ? "Saved. Notes without a vector of this model are being embedded in the background."
          : "Saved. With no embedding model, notes are found by their words only.",
      });
    } catch (caught) {
      onNotice({ kind: "error", text: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  }

  async function test() {
    setBusy("test");
    setResult(null);
    try {
      setResult(
        await api.testEmbedding({
          ...values(),
          api_key: apiKey || undefined,
          rerank_api_key: rerankKey || undefined,
        }),
      );
    } catch (caught) {
      setResult({
        ok: false,
        message: errorMessage(caught),
        dimensions: null,
        embed_ms: null,
        rerank_ok: null,
        rerank_message: null,
        rerank_ms: null,
      });
    } finally {
      setBusy(null);
    }
  }

  const disabled = !isAdmin;
  const isTei = form.provider === "tei";

  return (
    <Card
      title="Embedding model"
      action={config.data?.configured ? <Pill kind="ok">Set up</Pill> : <Pill>Not set up</Pill>}
    >
      <form className="stack" onSubmit={save}>
        <p className="muted">
          Turns notes and questions into vectors, so a note is found even when it says the same thing
          in other words. Shared with the knowledge base later. With an external gateway, the text of
          every note and question is sent to it.
        </p>
        <div className="grid">
          <Field label="Provider">
            <select
              value={form.provider}
              disabled={disabled}
              onChange={(event) => set("provider", event.target.value as EmbeddingForm["provider"])}
            >
              <option value="">None (words only)</option>
              <option value="openai">OpenAI-compatible gateway (…/embeddings)</option>
              <option value="tei">text-embeddings-inference (…/embed)</option>
            </select>
          </Field>
          <Field
            label="URL"
            hint={isTei ? "e.g. http://embed:80 for the bundled server." : "Up to the version, e.g. https://api.example.com/v1."}
          >
            <input
              type="url"
              value={form.base_url}
              disabled={disabled || !form.provider}
              placeholder={isTei ? "http://embed:80" : "https://api.example.com/v1"}
              onChange={(event) => set("base_url", event.target.value)}
            />
          </Field>
        </div>
        <div className="grid">
          <Field
            label="API key"
            hint={form.has_api_key ? "A key is stored (encrypted). Type a new one to replace it." : "Optional for a local server."}
          >
            <input
              type="password"
              autoComplete="off"
              placeholder={form.has_api_key ? "•••••••• (stored)" : ""}
              value={apiKey}
              disabled={disabled || !form.provider}
              onChange={(event) => setApiKey(event.target.value)}
            />
          </Field>
          <Field label="Model" hint={isTei ? "Optional: the server serves one model." : "e.g. text-embedding-3-small, bge-m3."}>
            <input
              type="text"
              value={form.model}
              disabled={disabled || !form.provider}
              onChange={(event) => set("model", event.target.value)}
            />
          </Field>
          <Field label="Dimensions" hint="Empty: the model's own size. Only for models that can shorten vectors.">
            <input
              type="number"
              min={8}
              max={8192}
              value={form.dimensions}
              disabled={disabled || form.provider !== "openai"}
              onChange={(event) => set("dimensions", event.target.value)}
            />
          </Field>
        </div>
        <div className="grid">
          <Field label="Question prefix" hint={<>e5 models: <code>query: </code></>}>
            <input
              type="text"
              value={form.query_prefix}
              disabled={disabled || !form.provider}
              onChange={(event) => set("query_prefix", event.target.value)}
            />
          </Field>
          <Field label="Note prefix" hint={<>e5 models: <code>passage: </code></>}>
            <input
              type="text"
              value={form.document_prefix}
              disabled={disabled || !form.provider}
              onChange={(event) => set("document_prefix", event.target.value)}
            />
          </Field>
          <Field label="Timeout (seconds)">
            <input
              type="number"
              min={1}
              max={300}
              value={form.timeout_seconds}
              disabled={disabled || !form.provider}
              onChange={(event) => set("timeout_seconds", Number(event.target.value))}
            />
          </Field>
        </div>

        <h3>Reranker (optional)</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          Reads the question with each result and reorders them. Better results, at the price of one
          more call (about 50 – 300 ms) per question.
        </p>
        <div className="grid">
          <Field label="Provider">
            <select
              value={form.rerank_provider}
              disabled={disabled}
              onChange={(event) => set("rerank_provider", event.target.value as EmbeddingForm["rerank_provider"])}
            >
              <option value="">None</option>
              <option value="cohere">Cohere / Jina / LiteLLM (…/rerank)</option>
              <option value="tei">text-embeddings-inference (…/rerank)</option>
            </select>
          </Field>
          <Field label="URL">
            <input
              type="url"
              value={form.rerank_base_url}
              disabled={disabled || !form.rerank_provider}
              onChange={(event) => set("rerank_base_url", event.target.value)}
            />
          </Field>
          <Field label="API key" hint={form.has_rerank_api_key ? "A key is stored (encrypted)." : undefined}>
            <input
              type="password"
              autoComplete="off"
              placeholder={form.has_rerank_api_key ? "•••••••• (stored)" : ""}
              value={rerankKey}
              disabled={disabled || !form.rerank_provider}
              onChange={(event) => setRerankKey(event.target.value)}
            />
          </Field>
          <Field label="Model">
            <input
              type="text"
              value={form.rerank_model}
              disabled={disabled || !form.rerank_provider}
              onChange={(event) => set("rerank_model", event.target.value)}
            />
          </Field>
        </div>

        {isAdmin && (
          <div className="row">
            <button className="primary" type="submit" disabled={busy !== null}>
              {busy === "save" ? "Saving…" : "Save"}
            </button>
            <button type="button" onClick={test} disabled={busy !== null || !form.provider}>
              {busy === "test" ? "Testing…" : "Test"}
            </button>
          </div>
        )}
        {result && (
          <>
            <Banner kind={result.ok ? "ok" : "error"}>{result.message}</Banner>
            {result.rerank_message && (
              <Banner kind={result.rerank_ok ? "ok" : "warn"}>
                {result.rerank_message}
                {result.rerank_ms != null && ` (${result.rerank_ms} ms)`}
              </Banner>
            )}
          </>
        )}
      </form>
    </Card>
  );
}

// ------------------------------------------------------------------------- //

function scopeLabel(scope: string, scopes: MemoryScope[]): string {
  return scopes.find((item) => item.scope === scope)?.label ?? scope;
}

function NotesCard({ onNotice, onChanged }: { onNotice: (n: Notice) => void; onChanged: () => void }) {
  const scopes = useResource(api.memoryScopes, []);
  const [scope, setScope] = useState("");
  const [filter, setFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const notes = useResource(() => api.listNotes({ scope, q: filter, offset, limit: PAGE_SIZE }), [scope, filter, offset]);
  const [editing, setEditing] = useState<{ id: number; text: string } | null>(null);
  const [newScope, setNewScope] = useState("global");
  const [newText, setNewText] = useState("");
  const [query, setQuery] = useState("");
  const [queryScope, setQueryScope] = useState("global");
  const [found, setFound] = useState<MemoryNote[] | null>(null);
  const [busy, setBusy] = useState(false);

  const known = scopes.data ?? [];

  async function run(action: () => Promise<void>) {
    setBusy(true);
    try {
      await action();
    } catch (caught) {
      onNotice({ kind: "error", text: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }

  const refresh = async () => {
    await Promise.all([notes.reload(), scopes.reload()]);
    onChanged();
  };

  const add = (event: React.FormEvent) => {
    event.preventDefault();
    void run(async () => {
      const note = await api.addNote(newScope.trim(), newText);
      setNewText("");
      await refresh();
      onNotice({ kind: "ok", text: `Saved note #${note.id}.` });
    });
  };

  const saveEdit = () =>
    editing &&
    void run(async () => {
      await api.updateNote(editing.id, editing.text);
      setEditing(null);
      await refresh();
    });

  const remove = (note: MemoryNote) =>
    void run(async () => {
      await api.deleteNote(note.id);
      await refresh();
      onNotice({ kind: "ok", text: `Forgot note #${note.id}.` });
    });

  const forgetScope = () =>
    scope &&
    window.confirm(`Forget all ${known.find((s) => s.scope === scope)?.count ?? ""} notes of ${scopeLabel(scope, known)}?`) &&
    void run(async () => {
      await api.forgetScope(scope);
      setScope("");
      await refresh();
    });

  const search = (event: React.FormEvent) => {
    event.preventDefault();
    const read = queryScope === "global" ? ["global"] : [queryScope, "global"];
    void run(async () => setFound(await api.searchNotes(query, read)));
  };

  const items = notes.data?.items ?? [];
  const total = notes.data?.total ?? 0;

  return (
    <Card title="Notes">
      <div className="stack">
        <div className="row">
          <select
            aria-label="Whose notes"
            value={scope}
            onChange={(event) => {
              setScope(event.target.value);
              setOffset(0);
            }}
          >
            <option value="">Everyone's notes</option>
            {known.map((item) => (
              <option key={item.scope} value={item.scope}>
                {item.label} ({item.count})
              </option>
            ))}
          </select>
          <input
            type="search"
            placeholder="Filter by text"
            aria-label="Filter notes"
            value={filter}
            onChange={(event) => {
              setFilter(event.target.value);
              setOffset(0);
            }}
          />
          {scope && (
            <button type="button" className="danger" disabled={busy} onClick={forgetScope}>
              Forget all of these
            </button>
          )}
        </div>

        {notes.loading && !notes.data ? (
          <Loading />
        ) : notes.error ? (
          <Banner kind="error">{notes.error}</Banner>
        ) : items.length === 0 ? (
          <Empty>No notes{filter || scope ? " match" : " yet. The model saves them as it chats, or add one below"}.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Whose</th>
                  <th>Note</th>
                  <th>From</th>
                  <th>Saved</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {items.map((note) => (
                  <tr key={note.id}>
                    <td className="muted">{note.id}</td>
                    <td>{scopeLabel(note.scope, known)}</td>
                    <td style={{ minWidth: "18rem" }}>
                      {editing?.id === note.id ? (
                        <textarea
                          rows={2}
                          value={editing.text}
                          onChange={(event) => setEditing({ id: note.id, text: event.target.value })}
                        />
                      ) : (
                        note.text
                      )}
                    </td>
                    <td className="muted">
                      {SOURCE_LABEL[note.source] ?? note.source}
                      {note.created_by && ` · ${note.source === "portal" ? note.created_by : `@${note.created_by}`}`}
                    </td>
                    <td className="muted">{formatDateTime(note.created_at)}</td>
                    <td>
                      <div className="row">
                        {editing?.id === note.id ? (
                          <>
                            <button type="button" className="primary" disabled={busy || !editing.text.trim()} onClick={saveEdit}>
                              Save
                            </button>
                            <button type="button" onClick={() => setEditing(null)}>
                              Cancel
                            </button>
                          </>
                        ) : (
                          <>
                            <button type="button" disabled={busy} onClick={() => setEditing({ id: note.id, text: note.text })}>
                              Edit
                            </button>
                            <button type="button" className="danger" disabled={busy} onClick={() => remove(note)}>
                              Forget
                            </button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {total > PAGE_SIZE && (
          <div className="row">
            <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
              Newer
            </button>
            <span className="muted">
              {offset + 1}–{Math.min(total, offset + PAGE_SIZE)} of {total}
            </span>
            <button type="button" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>
              Older
            </button>
          </div>
        )}

        <h3>Add a note</h3>
        <form className="grid" onSubmit={add}>
          <Field
            label="Whose"
            hint={<>"global" is seen in every conversation, but only the portal writes it. Or <code>user:&lt;id&gt;</code>, <code>room:&lt;id&gt;</code>.</>}
          >
            <input type="text" list="memory-scopes" value={newScope} onChange={(event) => setNewScope(event.target.value)} />
            <datalist id="memory-scopes">
              <option value="global" />
              {known.map((item) => (
                <option key={item.scope} value={item.scope}>
                  {item.label}
                </option>
              ))}
            </datalist>
          </Field>
          <div className="stack">
            <Field label="Note" hint="One short statement that makes sense on its own.">
              <textarea rows={2} maxLength={1000} value={newText} onChange={(event) => setNewText(event.target.value)} />
            </Field>
            <div className="row">
              <button className="primary" type="submit" disabled={busy || !newText.trim() || !newScope.trim()}>
                Add note
              </button>
            </div>
          </div>
        </form>

        <h3>Try a search</h3>
        <form className="row" onSubmit={search}>
          <input
            type="text"
            placeholder="A question, as someone would ask it"
            aria-label="Question"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <select aria-label="Asked in" value={queryScope} onChange={(event) => setQueryScope(event.target.value)}>
            <option value="global">asked by anyone (global notes)</option>
            {known
              .filter((item) => item.scope !== "global")
              .map((item) => (
                <option key={item.scope} value={item.scope}>
                  asked in the conversation of {item.label}
                </option>
              ))}
          </select>
          <button type="submit" disabled={busy || !query.trim()}>
            Search
          </button>
        </form>
        {found &&
          (found.length === 0 ? (
            <Empty>Nothing found.</Empty>
          ) : (
            <ol>
              {found.map((note) => (
                <li key={note.id}>
                  [#{note.id}] {note.text}{" "}
                  <span className="muted">
                    ({scopeLabel(note.scope, known)}
                    {note.score != null && `, score ${note.score.toFixed(3)}`})
                  </span>
                </li>
              ))}
            </ol>
          ))}
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------------- //

function ProfilesCard({ onNotice }: { onNotice: (n: Notice) => void }) {
  const profiles = useResource(api.listProfiles, []);
  const [editing, setEditing] = useState<(ProfileUpdate & { rc_user_id: string; isNew?: boolean }) | null>(null);
  const [busy, setBusy] = useState(false);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!editing) return;
    setBusy(true);
    const { rc_user_id, isNew: _isNew, ...changes } = editing;
    try {
      await api.updateProfile(rc_user_id.trim(), changes);
      setEditing(null);
      await profiles.reload();
      onNotice({ kind: "ok", text: "Profile saved." });
    } catch (caught) {
      onNotice({ kind: "error", text: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }

  const remove = (profile: Profile) =>
    void api
      .deleteProfile(profile.rc_user_id)
      .then(() => profiles.reload())
      .then(() => onNotice({ kind: "ok", text: `Deleted the profile of @${profile.username}. Their notes are kept.` }))
      .catch((caught) => onNotice({ kind: "error", text: errorMessage(caught) }));

  const list = profiles.data ?? [];
  const text = (key: keyof ProfileUpdate) => ({
    value: (editing?.[key] as string | undefined) ?? "",
    onChange: (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
      editing && setEditing({ ...editing, [key]: event.target.value }),
  });

  return (
    <Card
      title="Profiles"
      action={
        <button type="button" onClick={() => setEditing({ rc_user_id: "", isNew: true })}>
          Add profile
        </button>
      }
    >
      <div className="stack">
        <p className="muted">
          Shown to the model in that person's DMs only. People change their own by telling the bot, e.g.
          “call me anh Bo” or “my timezone is Asia/Ho_Chi_Minh”.
        </p>
        {profiles.loading && !profiles.data ? (
          <Loading />
        ) : profiles.error ? (
          <Banner kind="error">{profiles.error}</Banner>
        ) : list.length === 0 ? (
          <Empty>No profiles yet.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>User</th>
                  <th>Call them</th>
                  <th>Language</th>
                  <th>Timezone</th>
                  <th>About</th>
                  <th>Notes</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {list.map((profile) => (
                  <tr key={profile.rc_user_id}>
                    <td>
                      @{profile.username || "?"}
                      <div className="hint">{profile.rc_user_id}</div>
                    </td>
                    <td>{profile.display_name || "—"}</td>
                    <td>{profile.language || "—"}</td>
                    <td>{profile.timezone || "—"}</td>
                    <td className="muted">{profile.about || "—"}</td>
                    <td>{profile.notes}</td>
                    <td>
                      <div className="row">
                        <button type="button" onClick={() => setEditing({ ...profile })}>
                          Edit
                        </button>
                        <button type="button" className="danger" onClick={() => remove(profile)}>
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {editing && (
          <form className="stack" onSubmit={save}>
            <h3>{editing.isNew ? "New profile" : `Edit @${editing.username}`}</h3>
            <div className="grid">
              <Field label="Rocket.Chat user id" hint="From the user's info in Rocket.Chat administration.">
                <input
                  type="text"
                  required
                  disabled={!editing.isNew}
                  value={editing.rc_user_id}
                  onChange={(event) => setEditing({ ...editing, rc_user_id: event.target.value })}
                />
              </Field>
              <Field label="Username">
                <input type="text" {...text("username")} />
              </Field>
              <Field label="Call them">
                <input type="text" maxLength={128} {...text("display_name")} />
              </Field>
              <Field label="Language" hint="e.g. Vietnamese">
                <input type="text" maxLength={64} {...text("language")} />
              </Field>
              <Field label="Timezone" hint="e.g. Asia/Ho_Chi_Minh">
                <input type="text" maxLength={64} {...text("timezone")} />
              </Field>
            </div>
            <Field label="About" hint="Role, team, how they like answers.">
              <textarea rows={2} maxLength={1000} {...text("about")} />
            </Field>
            <div className="row">
              <button className="primary" type="submit" disabled={busy || !editing.rc_user_id.trim()}>
                Save profile
              </button>
              <button type="button" onClick={() => setEditing(null)}>
                Cancel
              </button>
            </div>
          </form>
        )}
      </div>
    </Card>
  );
}
