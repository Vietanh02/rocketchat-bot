import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { ImportOptions, ImportReport } from "../api/types";
import { formatRelative } from "../format";
import { errorMessage, usePoll, useResource } from "../hooks";
import { Banner, Card, Checkbox, Empty, Field, Pill } from "./ui";

type Notice = { kind: "ok" | "error" | "warn"; text: string } | null;

function summary(report: ImportReport): string {
  const parts = [
    report.added && `${report.added} added`,
    report.updated && `${report.updated} updated`,
    report.unchanged && `${report.unchanged} unchanged`,
    report.removed && `${report.removed} removed`,
    report.duplicates && `${report.duplicates} duplicates`,
    report.skipped && `${report.skipped} skipped (not documents)`,
    report.failures.length && `${report.failures.length} failed`,
  ].filter(Boolean);
  return parts.join(" · ") || "nothing to do";
}

/** Import a whole folder (a zipped Confluence export, or one copied to the server). */
export default function ImportCard({ isAdmin, onImported }: { isAdmin: boolean; onImported: () => void }) {
  const imports = useResource(api.kbImports, []);
  const [options, setOptions] = useState<ImportOptions>({ model_index: true, prune: true, attachments: false });
  const [zip, setZip] = useState<File | null>(null);
  const [folder, setFolder] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const input = useRef<HTMLInputElement>(null);

  const data = imports.data;
  const running = (data?.imports ?? []).some((r) => r.status === "running");
  const wasRunning = useRef(false);
  usePoll(() => {
    if (running) void imports.reload();
  }, 2000);
  // When an import finishes, the document lists above are stale.
  useEffect(() => {
    if (wasRunning.current && !running) onImported();
    wasRunning.current = running;
  }, [running, onImported]);

  const fail = (caught: unknown) => setNotice({ kind: "error", text: errorMessage(caught) });

  async function start(run: () => Promise<ImportReport>) {
    setBusy(true);
    setNotice(null);
    try {
      const report = await run();
      setNotice({ kind: "ok", text: `Importing into ${report.collection}… progress below.` });
      setZip(null);
      if (input.current) input.current.value = "";
      await imports.reload();
    } catch (caught) {
      fail(caught);
    } finally {
      setBusy(false);
    }
  }

  const remove = (name: string, documents: number) =>
    window.confirm(`Remove the ${documents} documents imported as “${name}”?`) &&
    void api
      .deleteCollection(name)
      .then(({ removed }) => {
        setNotice({ kind: "ok", text: `Removed ${removed} documents of ${name}.` });
        void imports.reload();
        onImported();
      })
      .catch(fail);

  return (
    <Card title="Import a folder">
      <div className="stack">
        <p className="hint" style={{ marginTop: 0 }}>
          A whole folder of documents becomes a <em>collection</em>, such as a Confluence space exported
          with <code>confluence-markdown-exporter</code> (<code>cme spaces &lt;space-url&gt;</code>). Pages
          keep their titles; links become words and images are dropped. Importing the same folder again
          under the same name syncs it: changed pages are replaced and removed pages removed.
        </p>
        {notice && <Banner kind={notice.kind}>{notice.text}</Banner>}

        {isAdmin && (
          <>
            <div className="grid">
              <Field label="Collection name" hint="Empty: the ZIP's or folder's name. Use the same name to sync.">
                <input
                  type="text"
                  maxLength={60}
                  value={options.collection ?? ""}
                  onChange={(e) => setOptions({ ...options, collection: e.target.value || undefined })}
                />
              </Field>
            </div>
            <div className="row">
              <Checkbox
                label="Index with the language model"
                checked={options.model_index}
                onChange={(value) => setOptions({ ...options, model_index: value })}
              />
              <Checkbox
                label="Remove pages no longer in the folder"
                checked={options.prune}
                onChange={(value) => setOptions({ ...options, prune: value })}
              />
              <Checkbox
                label="Include attachments (PDF, Word…)"
                checked={options.attachments}
                onChange={(value) => setOptions({ ...options, attachments: value })}
              />
            </div>
            <p className="hint" style={{ marginTop: "-0.5rem" }}>
              The language model groups each page into sections and writes keywords and likely
              questions; a page takes it seconds, so a large space keeps it busy a while (pages are
              searchable at once meanwhile). Attachments are named after the links to them.
            </p>

            <div className="grid">
              <Field label="Upload a ZIP" hint="Zip the export folder on your computer; up to 1 GB.">
                <input
                  ref={input}
                  type="file"
                  accept=".zip,application/zip"
                  onChange={(e) => setZip(e.target.files?.[0] ?? null)}
                />
              </Field>
              <Field
                label="Or a folder on the server"
                hint={data ? `Copied into ${data.import_dir} (kb_import/ next to docker-compose.yml).` : ""}
              >
                <select value={folder} onChange={(e) => setFolder(e.target.value)}>
                  <option value="">—</option>
                  {(data?.folders ?? []).map((f) => (
                    <option key={f.name} value={f.name}>
                      {f.name} ({f.files} files)
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <div className="row">
              <button
                type="button"
                className="primary"
                disabled={busy || !zip}
                onClick={() => zip && void start(() => api.importZip(zip, options))}
              >
                {busy && zip ? "Uploading…" : "Import the ZIP"}
              </button>
              <button
                type="button"
                disabled={busy || !folder}
                onClick={() => void start(() => api.importServerFolder(folder, options))}
              >
                Import the folder
              </button>
            </div>
          </>
        )}

        {(data?.imports ?? []).slice(0, 5).map((report) => (
          <div key={report.id}>
            <div className="row">
              <Pill kind={report.status === "done" ? "ok" : report.status === "failed" ? "error" : "warn"}>
                {report.status === "running" ? `${report.done}/${report.total}` : report.status}
              </Pill>
              <strong>{report.collection}</strong>
              <span className="muted">
                {report.origin} · {formatRelative(report.started_at)} · {summary(report)}
              </span>
            </div>
            {report.status === "running" && report.total > 0 && (
              <progress max={report.total} value={report.done} style={{ width: "100%" }} />
            )}
            {report.error && <Banner kind="error">{report.error}</Banner>}
            {report.failures.length > 0 && (
              <details>
                <summary className="hint">{report.failures.length} files could not be read</summary>
                <ul className="hint">
                  {report.failures.map((f) => (
                    <li key={f.path}>
                      <code>{f.path}</code>: {f.error}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        ))}

        <h3 style={{ margin: "0.5rem 0 0" }}>Collections</h3>
        {!data || data.collections.length === 0 ? (
          <Empty>No folders imported yet.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Collection</th>
                  <th>Documents</th>
                  <th>Passages</th>
                  {isAdmin && <th />}
                </tr>
              </thead>
              <tbody>
                {data.collections.map((c) => (
                  <tr key={c.name}>
                    <td>
                      <strong>{c.name}</strong>
                    </td>
                    <td>{c.documents}</td>
                    <td>{c.passages}</td>
                    {isAdmin && (
                      <td>
                        <button type="button" className="danger" disabled={running} onClick={() => remove(c.name, c.documents)}>
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
  );
}
