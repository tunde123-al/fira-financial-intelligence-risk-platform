import { useState } from "react";
import { api } from "../api";
import { Bars, Card, Kpi, Status, Table, fmt, useAsync } from "../components/ui";
import { pageCount, percent } from "../lib/workflow";

const GROUPS = ["", "malformed", "duplicate", "invalid", "referential"];
const PAGE = 20;

export default function DataQualityPage() {
  const ds = useAsync(() => api.get<any>("/api/data-quality/dataset"), []);
  const s = useAsync(() => api.get<any>("/api/data-quality/summary"), []);
  const [batch, setBatch] = useState("");
  const [group, setGroup] = useState("");
  const [code, setCode] = useState("");
  const [page, setPage] = useState(1);
  const qs = `?limit=${PAGE}&offset=${(page - 1) * PAGE}${batch ? `&batch_id=${batch}` : ""}${group ? `&reason_group=${group}` : ""}${code ? `&reason_code=${code}` : ""}`;
  const rej = useAsync(() => api.get<any>(`/api/data-quality/rejected${qs}`), [qs]);
  const [open, setOpen] = useState<number | null>(null);
  const t = s.data?.totals;
  const num = (v: number | null | undefined) => (v === null || v === undefined ? "n/a" : v.toLocaleString());
  return (
    <div className="page">
      <h2>Data quality</h2>
      <div className="notice">
        Every incoming transaction batch passes a validation gate before monitoring sees it. Rejected rows are kept (with a reason code and a
        sanitised copy) so nothing disappears silently. Figures below are computed from the batch ledger; "n/a" means the source never declared
        what it expected, so no coverage can be stated.
      </div>
      <Card title="Stored dataset audit (computed from the data on demand)">
        {ds.data ? (
          <>
            <div className="kpis">
              <Kpi label="Records processed" value={ds.data.records_processed.toLocaleString()} />
              <Kpi label="Valid" value={ds.data.valid.toLocaleString()} />
              <Kpi label="With issues" value={ds.data.with_issues.toLocaleString()} />
              <Kpi label="Quality score" value={ds.data.quality_score === null ? "n/a" : `${ds.data.quality_score}`} />
              <Kpi label="Dataset age (days)" value={ds.data.freshness.dataset_age_days} />
            </div>
            <Table
              rows={Object.entries(ds.data.checks).map(([k, v]: [string, any]) => ({ check: k, count: v.count, description: v.description }))}
              columns={[
                { key: "check", label: "Check" },
                { key: "count", label: "Records", align: "right" },
                { key: "description", label: "Rule" },
              ]}
            />
            <p className="muted small">
              Other tables: {JSON.stringify(ds.data.other_tables)}. {ds.data.freshness.note}. Computed in {ds.data.duration_ms} ms. The batch ledger below
              covers rows as they arrive; this audit covers what is stored.
            </p>
          </>
        ) : (
          <Status loading={ds.loading} error={ds.error} />
        )}
      </Card>
      {t ? (
        <>
          <div className="kpis">
            <Kpi label="Batches recorded" value={num(t.batches)} />
            <Kpi label="Expected (declared)" value={num(t.expected)} />
            <Kpi label="Received" value={num(t.received)} />
            <Kpi label="Processed" value={num(t.processed)} />
            <Kpi label="Rejected" value={num(t.rejected)} />
            <Kpi label="Duplicates" value={num(t.duplicates)} />
            <Kpi label="Malformed" value={num(t.malformed)} />
            <Kpi label="Late (accepted)" value={num(t.late)} />
            <Kpi label="Failed" value={num(t.failed)} />
            <Kpi label="Missing (vs expected)" value={num(t.missing)} />
            <Kpi label="Coverage" value={percent(t.coverage)} />
            <Kpi label="Processing success" value={percent(t.processing_success)} />
            <Kpi label="Quality score" value={t.quality_score === null ? "n/a" : `${t.quality_score}`} />
          </div>
          <details className="small muted"><summary>How these are defined</summary>
            <ul>{Object.entries(s.data.definitions).map(([k, v]) => <li key={k}><b>{k}</b>: {String(v)}</li>)}</ul>
          </details>
          <div className="grid2">
            <Card title="Rejections by reason code (click a row below to drill down)">
              {Object.keys(t.reasons).length ? (
                <Bars data={Object.entries(t.reasons).map(([k, v]) => ({ label: k, value: v as number }))} />
              ) : (
                <div className="muted">Nothing has been rejected.</div>
              )}
            </Card>
            <Card title="Rejections by group">
              {Object.keys(s.data.by_group).length ? (
                <Bars data={Object.entries(s.data.by_group).map(([k, v]) => ({ label: k, value: v as number }))} />
              ) : (
                <div className="muted">Nothing has been rejected.</div>
              )}
            </Card>
          </div>
          <Card title="Recent batches">
            <Table
              rows={s.data.recent_batches}
              columns={[
                { key: "batch_id", label: "Batch", render: (r) => <button className="link" onClick={() => { setBatch(r.batch_id); setPage(1); }}>{r.batch_id}</button> },
                { key: "source", label: "Source" },
                { key: "status", label: "Status" },
                { key: "finished_at", label: "Finished", render: (r) => fmt(r.finished_at) },
                { key: "expected_count", label: "Expected", align: "right" },
                { key: "received", label: "Received", align: "right" },
                { key: "processed", label: "Processed", align: "right" },
                { key: "rejected", label: "Rejected", align: "right" },
                { key: "late", label: "Late", align: "right" },
                { key: "missing", label: "Missing", align: "right" },
                { key: "created_by", label: "By" },
              ]}
            />
          </Card>
        </>
      ) : (
        <Status loading={s.loading} error={s.error} />
      )}
      <Card title="Rejected records">
        <div className="row wrap" style={{ marginBottom: 8 }}>
          <select value={group} onChange={(e) => { setGroup(e.target.value); setPage(1); }} aria-label="reason group">
            {GROUPS.map((g) => <option key={g} value={g}>{g || "all groups"}</option>)}
          </select>
          <input placeholder="reason code, e.g. DUPLICATE_ID" value={code} onChange={(e) => { setCode(e.target.value.toUpperCase()); setPage(1); }} style={{ width: 240 }} />
          <input placeholder="batch BAT-…" value={batch} onChange={(e) => { setBatch(e.target.value.toUpperCase()); setPage(1); }} style={{ width: 200 }} />
          <button onClick={() => { setGroup(""); setCode(""); setBatch(""); setPage(1); }}>Clear filters</button>
        </div>
        {rej.data ? (
          <>
            <Table
              rows={rej.data.items}
              empty="No rejected records match."
              columns={[
                { key: "batch_id", label: "Batch" },
                { key: "row_number", label: "Row", align: "right" },
                { key: "reason_code", label: "Code" },
                { key: "reason_group", label: "Group" },
                { key: "reason", label: "Reason" },
                { key: "transaction_id", label: "Transaction" },
                { key: "created_at", label: "When", render: (r) => fmt(r.created_at) },
                { key: "payload", label: "", render: (r) => <button className="link" onClick={() => setOpen(open === r.id ? null : r.id)}>{open === r.id ? "hide" : "row"}</button> },
              ]}
            />
            {open !== null && rej.data.items.filter((r: any) => r.id === open).map((r: any) => (
              <pre className="json" key={r.id}>{JSON.stringify(r.payload, null, 2)}</pre>
            ))}
            <div className="row" style={{ marginTop: 10 }}>
              <button disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>Previous</button>
              <span className="muted">Page {page} of {pageCount(rej.data.total, PAGE)} · {rej.data.total.toLocaleString()} record(s)</span>
              <button disabled={page >= pageCount(rej.data.total, PAGE)} onClick={() => setPage((p) => p + 1)}>Next</button>
            </div>
            <p className="muted small">Stored payloads are sanitised: IP addresses are reduced to a /16 prefix and text fields are truncated.</p>
          </>
        ) : (
          <Status loading={rej.loading} error={rej.error} />
        )}
      </Card>
    </div>
  );
}
