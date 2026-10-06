import { useState } from "react";
import { api } from "../api";
import FlowGraph from "../components/FlowGraph";
import { Badge, Bars, Card, Status, Table, fmt, useAsync } from "../components/ui";
import { go } from "../router";

const BAND_KIND: Record<string, string> = { HIGH: "high", MEDIUM: "medium", LOW: "low", NONE: "low" };

function Suspects() {
  const s = useAsync(() => api.get<any>("/api/mule/suspects?limit=25&min_band=LOW"), []);
  const p = useAsync(() => api.get<any>("/api/mule/patterns?min_degree=5&limit=25"), []);
  const [cust, setCust] = useState("");
  return (
    <div className="page">
      <h2>Money-mule view</h2>
      <div className="notice">
        These are money-mule <b>risk indicators</b>, not findings. Each indicator can occur in legitimate activity; a human investigator decides.
        Scores add up documented, evidence-backed indicators (max 100) and use no machine-learning model.
      </div>
      <Card title="Open a customer">
        <form className="row" onSubmit={(e) => { e.preventDefault(); if (cust.trim()) go(`/mule/${cust.trim().toUpperCase()}`); }}>
          <input placeholder="CUST-…" value={cust} onChange={(e) => setCust(e.target.value)} style={{ width: 160 }} />
          <button className="primary" disabled={!cust.trim()}>Investigate</button>
        </form>
      </Card>
      <Card title="Customers with fund-flow alerts, ranked by indicator score">
        {s.data ? (
          <>
            <Table
              rows={s.data.suspects}
              empty="No customers with fund-flow alerts. Run monitoring first."
              columns={[
                { key: "customer_id", label: "Customer", render: (r) => <a href={`#/mule/${r.customer_id}`}>{r.customer_id}</a> },
                { key: "score", label: "Indicator score", align: "right" },
                { key: "band", label: "Band", render: (r) => <Badge value={r.band} kind={BAND_KIND[r.band]} /> },
                { key: "fired", label: "Indicators that fired", render: (r) => (r.fired as string[]).join("; ") },
                { key: "inbound_usd", label: "In (USD)", align: "right" },
                { key: "outbound_usd", label: "Out (USD)", align: "right" },
              ]}
            />
            <p className="muted small">{s.data.scope}. Examined {s.data.examined} customer(s).</p>
          </>
        ) : (
          <Status loading={s.loading} error={s.error} />
        )}
      </Card>
      <Card title="Fan-in / fan-out accounts in the transfer graph (last 30 days)">
        {p.data ? (
          <Table
            rows={p.data.patterns}
            empty="No accounts with five or more distinct senders or receivers."
            columns={[
              { key: "account_id", label: "Account", render: (r) => <a href={`#/graph?kind=account&id=${r.account_id}`}>{r.account_id}</a> },
              { key: "owner_customer_id", label: "Owner", render: (r) => (r.owner_customer_id ? <a href={`#/mule/${r.owner_customer_id}`}>{r.owner_customer_id}</a> : "—") },
              { key: "pattern", label: "Pattern" },
              { key: "distinct_senders", label: "Senders", align: "right" },
              { key: "distinct_receivers", label: "Receivers", align: "right" },
              { key: "in_usd", label: "In (USD)", align: "right" },
              { key: "out_usd", label: "Out (USD)", align: "right" },
              { key: "flagged", label: "Open alert" },
            ]}
          />
        ) : (
          <Status loading={p.loading} error={p.error} />
        )}
      </Card>
    </div>
  );
}

function Investigate({ id }: { id: string }) {
  const [depth, setDepth] = useState(2);
  const [days, setDays] = useState(30);
  const a = useAsync(() => api.get<any>(`/api/mule/customers/${id}`), [id]);
  const f = useAsync(() => api.get<any>(`/api/mule/customers/${id}/flow?depth=${depth}&days=${days}`), [id, depth, days]);
  const flagged = (f.data?.nodes ?? []).filter((n: any) => n.flagged && n.depth > 0);
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h2>Money-mule indicators · <a href={`#/customers/${id}`}>{id}</a></h2>
          <p className="muted"><a href="#/mule">← all suspects</a></p>
        </div>
        {a.data && <div className="row"><Badge value={`${a.data.band} · ${a.data.score}/100`} kind={BAND_KIND[a.data.band]} /></div>}
      </div>
      {a.data ? (
        <>
          <div className="notice">{a.data.disclaimer} {a.data.context?.map((c: string) => <div key={c}>{c}</div>)}</div>
          <div className="grid2">
            <Card title="Indicators">
              <Bars data={a.data.indicators.map((i: any) => ({ label: i.label, value: i.points, note: `/ ${i.max}` }))} max={20} />
              <p className="muted small">Window {fmt(a.data.window[0])} → {fmt(a.data.window[1])}. Bands: HIGH ≥ {a.data.bands.high}, MEDIUM ≥ {a.data.bands.medium}, LOW ≥ {a.data.bands.low}.</p>
            </Card>
            <Card title="Why each indicator did or did not fire">
              <ul className="plain">
                {a.data.indicators.map((i: any) => (
                  <li key={i.id}>
                    <b>{i.fired ? "●" : "○"} {i.label}</b> — {i.reason}
                    {i.evidence?.transaction_ids?.length > 0 && (
                      <div className="muted small">transactions: {(i.evidence.transaction_ids as string[]).slice(0, 5).map((t) => <a key={t} href={`#/search?q=${t}`}>{t} </a>)}</div>
                    )}
                    {i.evidence?.customers?.length > 0 && (
                      <div className="muted small">customers: {(i.evidence.customers as string[]).map((c) => <a key={c} href={`#/mule/${c}`}>{c} </a>)}</div>
                    )}
                  </li>
                ))}
              </ul>
            </Card>
          </div>
        </>
      ) : (
        <Status loading={a.loading} error={a.error} />
      )}
      <Card
        title="Money flow"
        actions={
          <span className="row">
            <label className="radio">depth
              <select value={depth} onChange={(e) => setDepth(Number(e.target.value))} aria-label="depth">{[1, 2, 3].map((d) => <option key={d}>{d}</option>)}</select>
            </label>
            <label className="radio">days
              <select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="days">{[7, 30, 90].map((d) => <option key={d}>{d}</option>)}</select>
            </label>
          </span>
        }
      >
        {f.data ? (
          <>
            <FlowGraph nodes={f.data.nodes} edges={f.data.edges} />
            {f.data.truncated && <div className="notice">The view was cut at its size limit; reduce depth or days to see everything.</div>}
            {flagged.length > 0 && <p><b>Connected accounts whose owner has an open alert:</b> {flagged.map((n: any) => `${n.account_id} (${n.owner_customer_id})`).join(", ")}</p>}
            <Table
              rows={f.data.edges}
              empty="No transfers."
              columns={[
                { key: "source", label: "Source account" },
                { key: "target", label: "Destination account" },
                { key: "total_usd", label: "Amount (USD)", align: "right" },
                { key: "n", label: "Transfers", align: "right" },
                { key: "direction", label: "Direction", render: (r) => (r.direction === "in" ? "into the customer's side" : "out of the customer's side") },
                { key: "depth", label: "Depth", align: "right" },
                { key: "first_ts", label: "First", render: (r) => fmt(r.first_ts) },
                { key: "last_ts", label: "Last", render: (r) => fmt(r.last_ts) },
              ]}
            />
            <p className="muted small">{f.data.note}</p>
          </>
        ) : (
          <Status loading={f.loading} error={f.error} />
        )}
      </Card>
    </div>
  );
}

export default function MuleFlowPage({ id }: { id?: string }) {
  return id ? <Investigate id={id} /> : <Suspects />;
}
