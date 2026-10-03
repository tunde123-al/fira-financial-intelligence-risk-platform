import { FormEvent, useState } from "react";
import { Session, api, qs } from "../api";
import { Card, Status, Table, errorText, useAsync } from "../components/ui";

export default function DocumentsPage({ session }: { session: Session }) {
  const docs = useAsync(() => api.get<any[]>("/api/documents"), []);
  const [q, setQ] = useState("");
  const [mode, setMode] = useState("hybrid");
  const [res, setRes] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [file, setFile] = useState<File | null>(null);

  const search = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      setRes(await api.get(`/api/documents/search${qs({ q, k: 8, mode })}`));
    } catch (err) {
      setError(errorText(err));
    }
  };
  const ingest = async () => {
    setBusy(true);
    try {
      await api.post("/api/documents/ingest");
      docs.reload();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  const upload = async () => {
    if (!file) return;
    setBusy(true);
    const form = new FormData();
    form.append("file", file);
    try {
      await api.upload("/api/documents/upload", form);
      docs.reload();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="page">
      <h2>Document Search</h2>
      <form className="row" onSubmit={search}>
        <input style={{ flex: 1 }} placeholder="e.g. who may file a suspicious transaction report" value={q} onChange={(e) => setQ(e.target.value)} />
        <select value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="hybrid">hybrid</option>
          <option value="semantic">semantic</option>
          <option value="keyword">keyword</option>
        </select>
        <button className="primary" disabled={q.trim().length < 2}>
          Search
        </button>
      </form>
      {error && <div className="error">{error}</div>}
      {res && (
        <Card title={`${res.passages.length} passage(s)${res.semantic_available ? "" : " — semantic index unavailable, keyword only"}`}>
          {res.passages.map((p: any) => (
            <div key={p.chunk_id} className="passage">
              <h4>
                {p.title} <span className="muted small">({p.doc_type})</span>
              </h4>
              <p className="muted small">
                {p.section} · {p.document_id} · chunk {p.chunk_id}
                {p.page ? ` · page ${p.page}` : ""} · score {p.score.toFixed(4)} (semantic rank {p.semantic_rank ?? "–"}, keyword rank {p.keyword_rank ?? "–"})
              </p>
              <blockquote>{p.text}</blockquote>
              <p className="muted small">Source: {p.source}</p>
            </div>
          ))}
        </Card>
      )}
      <Card
        title="Document registry"
        actions={
          session.role === "admin" && (
            <div className="row">
              <input type="file" accept=".pdf,.docx,.md,.txt" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
              <button onClick={upload} disabled={!file || busy}>
                Upload &amp; ingest
              </button>
              <button onClick={ingest} disabled={busy}>
                {busy ? "Working…" : "Re-ingest all"}
              </button>
            </div>
          )
        }
      >
        {docs.data ? (
          <Table
            rows={docs.data}
            columns={[
              { key: "document_id", label: "Document" },
              { key: "title", label: "Title" },
              { key: "doc_type", label: "Type" },
              { key: "version", label: "Version" },
              { key: "page_count", label: "Pages" },
              { key: "parser", label: "Parser", render: (r) => r.metadata?.parser },
              { key: "ocr", label: "OCR pages", render: (r) => (r.metadata?.ocr_pages ?? []).join(", ") },
              { key: "source", label: "Source" },
            ]}
          />
        ) : (
          <Status loading={docs.loading} error={docs.error} />
        )}
      </Card>
    </div>
  );
}
