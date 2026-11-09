import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { AnswerEntry, CacheConfig, CacheTestResult } from "../api/types";
import { useAuth } from "../auth";
import { Banner, Card, Checkbox, Empty, Field, Loading, Pill, Stat } from "../components/ui";
import { formatDateTime, formatRelative, truncate } from "../format";
import { errorMessage, useResource } from "../hooks";

type Notice = { kind: "ok" | "error" | "warn"; text: string } | null;

interface Draft {
  id: number | null;
  question: string;
  /** One way of asking per line. */
  alternatives: string;
  answer: string;
}

const EMPTY_DRAFT: Draft = { id: null, question: "", alternatives: "", answer: "" };

const lines = (text: string) =>
  text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

const rate = (hits: number, misses: number) =>
  hits + misses === 0 ? "—" : `${Math.round((100 * hits) / (hits + misses))}%`;

export default function CachePage() {
  const { isAdmin } = useAuth();
  const config = useResource(api.getCacheConfig, []);
  const stats = useResource(api.cacheStats, []);
  const answers = useResource(() => api.listAnswers(), []);
  const [form, setForm] = useState<CacheConfig | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);
  const [question, setQuestion] = useState("");
  const [tried, setTried] = useState<CacheTestResult | null>(null);

  useEffect(() => {
    if (config.data) setForm(config.data);
  }, [config.data]);

  const refresh = () => {
    void stats.reload();
    void answers.reload();
  };

  const fail = (caught: unknown) => setNotice({ kind: "error", text: errorMessage(caught) });

  async function saveConfig(event: React.FormEvent) {
    event.preventDefault();
    if (!form) return;
    setBusy(true);
    try {
      config.setData(await api.updateCacheConfig(form));
      setNotice({ kind: "ok", text: "Saved." });
    } catch (caught) {
      fail(caught);
    } finally {
      setBusy(false);
    }
  }

  async function saveDraft(event: React.FormEvent) {
    event.preventDefault();
    if (!draft) return;
    setBusy(true);
    const body = { question: draft.question, alternatives: lines(draft.alternatives), answer: draft.answer };
    try {
      if (draft.id === null) await api.pinAnswer(body);
      else await api.updateAnswer(draft.id, body);
      setDraft(null);
      refresh();
      setNotice({ kind: "ok", text: "Pinned. It is given to that question from now on." });
    } catch (caught) {
      fail(caught);
    } finally {
      setBusy(false);
    }
  }

  const keep = (entry: AnswerEntry) =>
    void api
      .updateAnswer(entry.id, {})
      .then(() => {
        refresh();
        setNotice({ kind: "ok", text: "Pinned: it no longer expires or goes when the documents change." });
      })
      .catch(fail);

  const remove = (entry: AnswerEntry) =>
    window.confirm(`Remove the answer to “${truncate(entry.question, 80)}”?`) &&
    void api
      .deleteAnswer(entry.id)
      .then(() => {
        refresh();
        setNotice({ kind: "ok", text: "Removed." });
      })
      .catch(fail);

  const clearSaved = () =>
    window.confirm("Remove every saved answer? Pinned answers stay.") &&
    void api
      .clearSavedAnswers()
      .then(({ removed }) => {
        refresh();
        setNotice({ kind: "ok", text: `Removed ${removed} saved answers.` });
      })
      .catch(fail);

  async function tryQuestion(event: React.FormEvent) {
    event.preventDefault();
    setTried(null);
    try {
      setTried(await api.testCache(question));
    } catch (caught) {
      fail(caught);
    }
  }

  const entries = answers.data ?? [];
  const pinned = entries.filter((entry) => entry.kind === "pinned");
  const saved = entries.filter((entry) => entry.kind === "auto");

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Cache</h1>
          <p>
            Answers given again, without the model, to questions asked before: in well under a second.
            Questions are looked up in the <Link to="/knowledge">knowledge base</Link> first; when the
            answer came from it alone, it is kept for the next person who asks the same thing.
          </p>
        </div>
        {isAdmin && (
          <div className="row">
            <button type="button" onClick={() => setDraft({ ...EMPTY_DRAFT })}>
              Pin an answer
            </button>
          </div>
        )}
      </div>

      {notice && <Banner kind={notice.kind}>{notice.text}</Banner>}
      {!isAdmin && <Banner kind="warn">You have read-only access; changes cannot be saved.</Banner>}

      <Card title="State" action={<button type="button" onClick={refresh}>Refresh</button>}>
        {stats.error ? (
          <Banner kind="error">{stats.error}</Banner>
        ) : !stats.data ? (
          <Loading />
        ) : (
          <div className="grid">
            <Stat label="Pinned answers" value={stats.data.pinned} />
            <Stat label="Saved answers" value={stats.data.saved} />
            <Stat label="Given since start" value={stats.data.served} />
            <Stat
              label="Lookups reused"
              value={`${rate(stats.data.lookups.hits, stats.data.lookups.misses)} of ${
                stats.data.lookups.hits + stats.data.lookups.misses
              }`}
            />
            <Stat
              label="Integration answers reused"
              value={`${stats.data.tool_results.hits} (${stats.data.tool_results.entries} kept)`}
            />
          </div>
        )}
        <p className="hint">
          Lookups (the passages found for a question, and the model's choice among them) are kept 10
          minutes, and forgotten when a document changes. Integrations reuse answers only when their{" "}
          <Link to="/integrations">“Reuse answers for”</Link> is set.
        </p>
      </Card>

      {draft && (
        <form onSubmit={saveDraft}>
          <Card title={draft.id === null ? "Pin an answer" : "Change the answer"}>
            <div className="stack">
              <Field label="Question">
                <input
                  type="text"
                  required
                  maxLength={500}
                  value={draft.question}
                  onChange={(event) => setDraft({ ...draft, question: event.target.value })}
                />
              </Field>
              <Field
                label="Other ways of asking it"
                hint="One per line. Matched with case and spacing ignored, and by meaning when an embedding model is set up."
              >
                <textarea
                  rows={3}
                  value={draft.alternatives}
                  onChange={(event) => setDraft({ ...draft, alternatives: event.target.value })}
                />
              </Field>
              <Field label="Answer" hint="Markdown, as the model would write it.">
                <textarea
                  rows={5}
                  required
                  value={draft.answer}
                  onChange={(event) => setDraft({ ...draft, answer: event.target.value })}
                />
              </Field>
              <div className="row">
                <button type="submit" className="primary" disabled={busy}>
                  {busy ? "Saving…" : "Pin"}
                </button>
                <button type="button" onClick={() => setDraft(null)}>
                  Cancel
                </button>
              </div>
            </div>
          </Card>
        </form>
      )}

      <Card title="Try a question">
        <form onSubmit={tryQuestion} className="stack">
          <Field label="Question" hint="What it would be given from kept answers; nothing is counted.">
            <input type="text" value={question} onChange={(event) => setQuestion(event.target.value)} />
          </Field>
          <div className="row">
            <button type="submit" disabled={!question.trim()}>
              Look up
            </button>
          </div>
          {tried &&
            (tried.found ? (
              <div>
                <Banner kind="ok">
                  {tried.kind === "pinned" ? "Pinned" : "Saved"} answer #{tried.entry_id}, matched{" "}
                  {tried.matched_by === "exact"
                    ? "exactly"
                    : `by meaning (${(tried.score ?? 0).toFixed(3)})`}
                  .
                </Banner>
                <pre>{tried.reply}</pre>
              </div>
            ) : (
              <Banner kind="warn">Nothing kept for it: the assistant would work it out.</Banner>
            ))}
        </form>
      </Card>

      <Card title="Pinned answers">
        <Answers
          resource={answers}
          entries={pinned}
          isAdmin={isAdmin}
          empty="No pinned answers. Pin the questions people ask most, or keep a saved answer."
          onEdit={(entry) =>
            setDraft({
              id: entry.id,
              question: entry.question,
              alternatives: entry.alternatives.join("\n"),
              answer: entry.answer,
            })
          }
          onDelete={remove}
        />
      </Card>

      <Card
        title="Saved answers"
        action={
          isAdmin &&
          saved.length > 0 && (
            <button type="button" className="danger" onClick={clearSaved}>
              Remove all
            </button>
          )
        }
      >
        <p className="hint">
          Kept after an answer from the knowledge base alone. The fast model rewrites the question to
          stand on its own and answers it from the passages only, so nothing of the person who asked is
          kept. A saved answer goes when a document changes, when it expires, or when someone reacts
          👎 to it in chat (the question is then worked out afresh).
        </p>
        <Answers
          resource={answers}
          entries={saved}
          isAdmin={isAdmin}
          empty="No saved answers yet."
          onKeep={keep}
          onDelete={remove}
        />
      </Card>

      {form && (
        <form onSubmit={saveConfig}>
          <Card title="Settings">
            <div className="stack">
              <Checkbox
                label="Save answers to knowledge-base questions"
                checked={form.answer_cache}
                disabled={!isAdmin}
                onChange={(value) => setForm({ ...form, answer_cache: value })}
              />
              <p className="hint" style={{ marginTop: "-0.5rem" }}>
                Off: nothing new is saved and saved answers are not given. Pinned answers always are.
              </p>
              <div className="grid">
                <Field
                  label="Similarity for saved answers"
                  hint="0.5 – 1. How close a question must be to a saved one, by meaning. High: only the same question in other words."
                >
                  <input
                    type="number"
                    min={0.5}
                    max={1}
                    step={0.01}
                    value={form.answer_min_similarity}
                    disabled={!isAdmin}
                    onChange={(event) => setForm({ ...form, answer_min_similarity: Number(event.target.value) })}
                  />
                </Field>
                <Field label="Similarity for pinned answers" hint="Usually a little lower: admins chose them.">
                  <input
                    type="number"
                    min={0.5}
                    max={1}
                    step={0.01}
                    value={form.pinned_min_similarity}
                    disabled={!isAdmin}
                    onChange={(event) => setForm({ ...form, pinned_min_similarity: Number(event.target.value) })}
                  />
                </Field>
                <Field label="Saved answers last (hours)" hint="168 = a week.">
                  <input
                    type="number"
                    min={1}
                    max={8760}
                    value={form.answer_ttl_hours}
                    disabled={!isAdmin}
                    onChange={(event) => setForm({ ...form, answer_ttl_hours: Number(event.target.value) })}
                  />
                </Field>
              </div>
              {isAdmin && (
                <div className="row">
                  <button type="submit" className="primary" disabled={busy}>
                    Save settings
                  </button>
                </div>
              )}
            </div>
          </Card>
        </form>
      )}
    </div>
  );
}

function Answers({
  resource,
  entries,
  isAdmin,
  empty,
  onEdit,
  onKeep,
  onDelete,
}: {
  resource: { loading: boolean; error: string | null; data: AnswerEntry[] | null };
  entries: AnswerEntry[];
  isAdmin: boolean;
  empty: string;
  onEdit?: (entry: AnswerEntry) => void;
  onKeep?: (entry: AnswerEntry) => void;
  onDelete: (entry: AnswerEntry) => void;
}) {
  if (resource.loading && !resource.data) return <Loading />;
  if (resource.error) return <Banner kind="error">{resource.error}</Banner>;
  if (entries.length === 0) return <Empty>{empty}</Empty>;
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Question</th>
            <th>Answer</th>
            <th>Given</th>
            <th>{onKeep ? "Expires" : "Added"}</th>
            {isAdmin && <th />}
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => (
            <tr key={entry.id}>
              <td>
                {entry.question}
                {entry.alternatives.length > 0 && (
                  <div className="hint">also: {entry.alternatives.join(" · ")}</div>
                )}
                {!entry.embedded && <div className="hint">matched exactly only, for now (no vector yet)</div>}
              </td>
              <td title={entry.answer}>
                {truncate(entry.answer, 160)}
                {entry.sources.length > 0 && (
                  <div className="hint">{entry.sources.map((s) => `[${s.number}] ${s.label}`).join(" · ")}</div>
                )}
              </td>
              <td>
                {entry.hits}×
                {entry.last_hit_at && <div className="hint">last {formatRelative(entry.last_hit_at)}</div>}
                {entry.downvotes > 0 && (
                  <div>
                    <Pill kind="warn">{entry.downvotes} 👎</Pill>
                  </div>
                )}
              </td>
              <td className="muted">
                {onKeep ? formatDateTime(entry.expires_at) : formatDateTime(entry.created_at)}
                {entry.created_by && <div className="hint">{entry.created_by}</div>}
              </td>
              {isAdmin && (
                <td>
                  <div className="row">
                    {onEdit && (
                      <button type="button" onClick={() => onEdit(entry)}>
                        Edit
                      </button>
                    )}
                    {onKeep && (
                      <button type="button" onClick={() => onKeep(entry)} title="Pin it: keep it until removed">
                        Pin
                      </button>
                    )}
                    <button type="button" className="danger" onClick={() => onDelete(entry)}>
                      Remove
                    </button>
                  </div>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
