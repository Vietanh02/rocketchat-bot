import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { KbDocument, KbSearchResult, KbUploadResult, KnowledgeConfig } from "../api/types";
import { useAuth } from "../auth";
import { Banner, Card, Checkbox, Empty, Field, Loading, Pill, Stat } from "../components/ui";
import { formatDateTime } from "../format";
import { errorMessage, useResource } from "../hooks";

type Notice = { kind: "ok" | "error" | "warn"; text: string } | null;

const ACCEPT = ".pdf,.docx,.md,.markdown,.txt,.csv,.tsv,.json,.html,.htm,.rst,.yaml,.yml,.log";

const INTENT_LABEL: Record<string, string> = {
  lookup: "question to look up",
  command: "request to do something",
  chat: "small talk",
};

function size(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** "conv:dm:<room>" → "DM <room>", for documents sent in chat. */
function conversationLabel(scope: string): string {
  const key = scope.replace(/^conv:/, "");
  if (key.startsWith("dm:")) return `DM ${key.slice(3)}`;
  const thread = key.match(/^room:([^:]+):t:(.+)$/);
  if (thread) return `room ${thread[1]}, thread ${thread[2]}`;
  return key.replace(/^room:/, "room ");
}

export default function KnowledgePage() {
  const { isAdmin } = useAuth();
  const status = useResource(api.knowledgeStatus, []);
  const base = useResource(() => api.listDocuments("portal"), []);
  const chat = useResource(() => api.listDocuments("chat"), []);
  const [notice, setNotice] = useState<Notice>(null);

  const refresh = () => {
    void status.reload();
    void base.reload();
    void chat.reload();
  };

  const remove = (document: KbDocument) =>
    window.confirm(`Delete “${document.filename}” and its ${document.chunks} passages?`) &&
    void api
      .deleteDocument(document.id)
      .then(() => {
        refresh();
        setNotice({ kind: "ok", text: `Deleted ${document.filename}.` });
      })
      .catch((caught) => setNotice({ kind: "error", text: errorMessage(caught) }));

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Knowledge base</h1>
          <p>
            Documents the assistant looks questions up in. In a conversation where someone sent
            documents of their own, those are searched first; otherwise, or when they say nothing
            about the question, these are.
          </p>
        </div>
      </div>

      {notice && <Banner kind={notice.kind}>{notice.text}</Banner>}
      {!isAdmin && <Banner kind="warn">You have read-only access; changes cannot be saved.</Banner>}

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
                  .reindexKnowledge()
                  .then((next) => {
                    status.setData(next);
                    void base.reload();
                  })
                  .catch((caught) => setNotice({ kind: "error", text: errorMessage(caught) }))
              }
            >
              Embed {status.data.pending} waiting passages
            </button>
          )
        }
      >
        {status.error ? (
          <Banner kind="error">{status.error}</Banner>
        ) : !status.data ? (
          <Loading />
        ) : (
          <div className="stack">
            <div className="grid">
              <Stat label="Documents" value={status.data.documents} />
              <Stat label="Sent in chat" value={status.data.chat_documents} />
              <Stat label="Passages" value={status.data.passages} />
              <Stat label="Waiting for a vector" value={status.data.semantic ? status.data.pending : "—"} />
            </div>
            <div>
              Search:{" "}
              {status.data.semantic ? (
                <Pill kind="ok">by meaning and words</Pill>
              ) : (
                <>
                  <Pill kind="warn">by words only</Pill>{" "}
                  <span className="hint">
                    Set up an embedding model on the <Link to="/memory">Memory</Link> page to also find
                    passages that say the same thing in other words.
                  </span>
                </>
              )}
            </div>
          </div>
        )}
      </Card>

      <BehaviourCard isAdmin={isAdmin} onNotice={setNotice} />

      {isAdmin && <UploadCard onUploaded={refresh} />}

      <Card title="Documents">
        <DocumentTable resource={base} isAdmin={isAdmin} onDelete={remove} empty="No documents yet. Upload some above." />
      </Card>

      <Card title="Sent in chat">
        <p className="muted" style={{ marginTop: 0 }}>
          Read only in the conversation they were sent in, and forgotten when it starts afresh
          (<code>new</code>).
        </p>
        <DocumentTable
          resource={chat}
          isAdmin={isAdmin}
          onDelete={remove}
          showConversation
          empty="Nobody has sent a document yet."
        />
      </Card>

      <TryCard />
    </div>
  );
}

// ------------------------------------------------------------------------- //

function BehaviourCard({ isAdmin, onNotice }: { isAdmin: boolean; onNotice: (n: Notice) => void }) {
  const config = useResource(api.getKnowledgeConfig, []);
  const [form, setForm] = useState<KnowledgeConfig | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (config.data) setForm(config.data);
  }, [config.data]);

  if (!form) return <Card title="Behaviour">{config.error ? <Banner kind="error">{config.error}</Banner> : <Loading />}</Card>;
  const set = <K extends keyof KnowledgeConfig>(key: K, value: KnowledgeConfig[K]) => setForm({ ...form, [key]: value });

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      config.setData(await api.updateKnowledgeConfig(form!));
      onNotice({ kind: "ok", text: "Saved. It applies to the next message." });
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
          label="Look questions up in the documents"
          checked={form.enabled}
          disabled={!isAdmin}
          onChange={(value) => set("enabled", value)}
        />
        <Checkbox
          label="Read documents people send in chat"
          checked={form.chat_uploads}
          disabled={!isAdmin || !form.enabled}
          onChange={(value) => set("chat_uploads", value)}
        />
        <p className="hint" style={{ marginTop: "-0.5rem" }}>
          In DMs, and in channels when the bot is mentioned. A file sent with a question is read and
          the question answered from it; a file sent alone is read and confirmed.
        </p>

        <Field
          label="Telling questions from requests"
          hint={
            <>
              A <b>question to look up</b> ("nghỉ phép được bao nhiêu ngày?") gets passages from the
              documents, to answer from and cite. A <b>request to do something</b> ("tạo task họp lúc
              9h") gets none, and the model is told to act with its tools. Small talk gets neither.
            </>
          }
        >
          <select
            value={form.intent_mode}
            disabled={!isAdmin || !form.enabled}
            onChange={(event) => set("intent_mode", event.target.value as KnowledgeConfig["intent_mode"])}
          >
            <option value="rules">By the wording (instant)</option>
            <option value="model">By the wording, then the fast model when unclear (+ ~0.5 s)</option>
          </select>
        </Field>

        <div className="grid">
          <Field label="Passages per question" hint="Put in front of a question to look up. 1 – 15.">
            <input
              type="number"
              min={1}
              max={15}
              value={form.top_k}
              disabled={!isAdmin}
              onChange={(event) => set("top_k", Number(event.target.value))}
            />
          </Field>
          <Field label="Minimum similarity" hint="0 – 1. For passages found only by meaning.">
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

function UploadCard({ onUploaded }: { onUploaded: () => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<KbUploadResult[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function upload(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setResults(null);
    try {
      setResults(await api.uploadDocuments(files));
      setFiles([]);
      if (input.current) input.current.value = "";
      onUploaded();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Upload">
      <form className="stack" onSubmit={upload}>
        <Field
          label="Files"
          hint="PDF (with a text layer, not scans), Word .docx, Markdown, text, CSV, JSON or HTML; up to 20 MB each, 20 at a time."
        >
          <input
            ref={input}
            type="file"
            multiple
            accept={ACCEPT}
            onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
          />
        </Field>
        <div className="row">
          <button className="primary" type="submit" disabled={busy || files.length === 0}>
            {busy ? "Reading…" : files.length > 1 ? `Upload ${files.length} files` : "Upload"}
          </button>
        </div>
        {error && <Banner kind="error">{error}</Banner>}
        {results && (
          <ul>
            {results.map((result, index) => (
              <li key={`${result.filename}-${index}`}>
                <Pill kind={result.ok ? "ok" : "warn"}>{result.ok ? "added" : "skipped"}</Pill> {result.filename}:{" "}
                <span className="muted">{result.message}</span>
              </li>
            ))}
          </ul>
        )}
      </form>
    </Card>
  );
}

// ------------------------------------------------------------------------- //

function DocumentTable({
  resource,
  isAdmin,
  onDelete,
  showConversation = false,
  empty,
}: {
  resource: { data: KbDocument[] | null; loading: boolean; error: string | null };
  isAdmin: boolean;
  onDelete: (document: KbDocument) => void;
  showConversation?: boolean;
  empty: string;
}) {
  if (resource.loading && !resource.data) return <Loading />;
  if (resource.error) return <Banner kind="error">{resource.error}</Banner>;
  const documents = resource.data ?? [];
  if (documents.length === 0) return <Empty>{empty}</Empty>;
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Document</th>
            {showConversation && <th>Conversation</th>}
            <th>Size</th>
            <th>Passages</th>
            <th>Added</th>
            {isAdmin && <th />}
          </tr>
        </thead>
        <tbody>
          {documents.map((document) => (
            <tr key={document.id}>
              <td>
                {document.title}
                <div className="hint">
                  {document.filename}
                  {document.truncated && " · cut short (very long)"}
                </div>
              </td>
              {showConversation && <td className="muted">{conversationLabel(document.scope)}</td>}
              <td className="muted">
                {size(document.size_bytes)}
                {document.pages ? ` · ${document.pages} pages` : ""}
              </td>
              <td>
                {document.chunks}
                {document.pending > 0 && <div className="hint">{document.pending} without a vector yet</div>}
              </td>
              <td className="muted">
                {formatDateTime(document.created_at)}
                {document.uploaded_by && (
                  <div className="hint">
                    {document.source === "chat" ? `@${document.uploaded_by}` : document.uploaded_by}
                  </div>
                )}
              </td>
              {isAdmin && (
                <td>
                  <button type="button" className="danger" onClick={() => onDelete(document)}>
                    Delete
                  </button>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------------------- //

function TryCard() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<KbSearchResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function run(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setResult(await api.searchKnowledge(question));
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Try a message">
      <form className="stack" onSubmit={run}>
        <div className="row">
          <input
            type="text"
            placeholder="e.g. Nhân viên được nghỉ phép bao nhiêu ngày?"
            aria-label="Message"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
          />
          <button type="submit" disabled={busy || !question.trim()}>
            Try
          </button>
        </div>
        {error && <Banner kind="error">{error}</Banner>}
        {result && (
          <div className="stack">
            <div>
              Read as a{" "}
              <Pill kind={result.intent === "lookup" ? "ok" : undefined}>
                {INTENT_LABEL[result.intent] ?? result.intent}
              </Pill>{" "}
              <span className="hint">
                ({result.intent_by === "model" ? "by the fast model" : result.intent_by === "default" ? "unclear wording, looked up" : "by the wording"})
              </span>
            </div>
            {result.intent !== "lookup" ? (
              <p className="muted">Such a message is not looked up; below is what a search would find anyway.</p>
            ) : null}
            {result.passages.length === 0 ? (
              <Empty>No passage of the knowledge base matches.</Empty>
            ) : (
              <ol>
                {result.passages.map((passage) => (
                  <li key={passage.id}>
                    <b>{passage.label}</b>
                    {passage.score != null && <span className="hint"> · score {passage.score.toFixed(3)}</span>}
                    <pre style={{ whiteSpace: "pre-wrap" }}>{passage.text}</pre>
                  </li>
                ))}
              </ol>
            )}
          </div>
        )}
      </form>
    </Card>
  );
}
