import { useMemo, useState } from "react";
import { Session, api } from "../api";
import Copilot from "../components/Copilot";
import GraphView from "../components/GraphView";
import { Badge, Bars, Card, Field, JsonBlock, ScoreBadge, Status, Table, Tabs, errorText, fmt, useAsync } from "../components/ui";
import { entityHref } from "../router";

const TABS = ["Report", "Risk Signals", "Transactions", "Graph", "Documents", "Evidence", "Agent Activity", "Timeline"];
const DECISIONS: [string, string][] = [
  ["confirm", "Confirm"],
  ["reject", "Reject"],
  ["escalate", "Escalate"],
  ["request_more_evidence", "Request More Evidence"],
];
const FAILURES = ["false_positive", "false_negative", "missing_evidence", "wrong_citation", "unclear_report", "wrong_subject", "other"];

function Claims({ items, onCite }: { items: any[]; onCite: (ref: string) => void }) {
  if (!items?.length) return <p className="muted">None.</p>;
  return (
    <ul className="claims">
      {items.map((c, i) => (
        <li key={i}>
          {c.text}{" "}
          {(c.citations ?? []).map((r: string) => (
            <button key={r} className="cite" onClick={() => onCite(r)} title="Show evidence">
              {r}
            </button>
          ))}
          <span className={`kind kind-${c.kind}`}>{c.kind}</span>
          {c.source && c.source !== "deterministic" && <span className="kind">{c.source}</span>}
        </li>
      ))}
    </ul>
  );
}

export default function InvestigationPage({ id, session }: { id: string; session: Session }) {
  const inv = useAsync(() => api.get(`/api/investigations/${id}`), [id]);
  const evidence = useAsync(() => api.get<any[]>(`/api/investigations/${id}/evidence`), [id]);
  const episodes = useAsync(() => api.get<any[]>(`/api/investigations/${id}/episodes`), [id]);
  const graph = useAsync(() => api.get(`/api/investigations/${id}/graph`).catch(() => ({ nodes: [], edges: [] })), [id]);
  const [tab, setTab] = useState("Report");
  const [focus, setFocus] = useState<string | null>(null);
  const [decision, setDecision] = useState("confirm");
  const [rationale, setRationale] = useState("");
  const [fails, setFails] = useState<string[]>([]);
  const [quality, setQuality] = useState(4);
  const [msg, setMsg] = useState<string | null>(null);
  const [rerunning, setRerunning] = useState(false);

  const evByRef = useMemo(() => new Map<string, any>((evidence.data ?? []).map((e: any) => [e.ref, e])), [evidence.data]);
  if (!inv.data) return <Status loading={inv.loading} error={inv.error} />;
  const d = inv.data;
  const r = d.report ?? {};
  const cite = (ref: string) => {
    setFocus(ref);
    setTab("Evidence");
  };

  const submitDecision = async () => {
    setMsg(null);
    try {
      await api.post(`/api/investigations/${id}/decision`, { decision, rationale, failure_categories: fails, report_quality: quality });
      setMsg("Decision recorded.");
      inv.reload();
      episodes.reload();
    } catch (e) {
      setMsg(errorText(e));
    }
  };

  const rerun = async () => {
    setRerunning(true);
    try {
      await api.post("/api/agent/investigate", { request: d.request_text ?? `Investigate ${d.subject_type} ${d.subject_id}`, subject_type: d.subject_type, subject_id: d.subject_id, investigation_id: id });
      inv.reload();
      evidence.reload();
      episodes.reload();
      graph.reload();
    } catch (e) {
      setMsg(errorText(e));
    } finally {
      setRerunning(false);
    }
  };

  const ra = r.risk_assessment ?? {};
  const unc = r.uncertainty ?? {};
  const timeline: { t: string; what: string }[] = [
    { t: d.created_at, what: `Investigation opened by ${d.created_by ?? "system"}` },
    ...(episodes.data ?? []).flatMap((ep: any) => [
      { t: ep.started_at, what: `Agent run ${ep.episode_id} started (${(ep.tool_calls ?? []).length} tool calls)` },
      { t: ep.finished_at, what: `Agent run finished: ${ep.status}` },
    ]),
    ...(d.decisions ?? []).map((x: any) => ({ t: x.created_at, what: `Decision "${x.decision}" by ${x.decided_by}: ${x.rationale}` })),
    ...(d.closed_at ? [{ t: d.closed_at, what: `Closed: ${d.conclusion}` }] : []),
  ]
    .filter((x) => x.t)
    .sort((a, b) => String(a.t).localeCompare(String(b.t)));

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h2>
            {d.investigation_id} <Badge value={d.status} /> {d.conclusion && <Badge value={d.conclusion} />}
          </h2>
          <p className="muted">
            Subject <a href={entityHref(d.subject_type, d.subject_id)}>{d.subject_type} {d.subject_id}</a>
            {r.analysed_customer && r.analysed_customer !== d.subject_id && (
              <>
                {" "}
                · analysed customer <a href={entityHref("customer", r.analysed_customer)}>{r.analysed_customer}</a>
              </>
            )}
            {r.window && ` · window ${fmt(r.window.start)} → ${fmt(r.window.end)}`}
          </p>
        </div>
        <div className="row">
          <ScoreBadge score={d.risk_score} band={ra.band} />
          <button onClick={rerun} disabled={rerunning}>
            {rerunning ? "Re-running…" : "Re-run agent"}
          </button>
          <a href={`/api/investigations/${id}/report.md`} onClick={async (e) => { e.preventDefault(); const t = await api.text(`/api/investigations/${id}/report.md`); const w = window.open(); if (w) { w.document.body.innerText = t; } }}>
            Markdown
          </a>
        </div>
      </div>
      {r.disclaimer && <div className="notice">{r.disclaimer}</div>}
      <Tabs tabs={TABS} current={tab} onChange={setTab} />

      {tab === "Report" && (
        <>
          {d.status === "needs_input" || d.status === "failed" ? (
            <Card title="Outcome">
              <Claims items={r.executive_summary ?? []} onCite={cite} />
            </Card>
          ) : (
            <>
              <Card title="Executive Summary">
                <Claims items={r.executive_summary} onCite={cite} />
              </Card>
              <Card title="Risk Indicators">
                <Table
                  rows={r.risk_indicators ?? []}
                  columns={[
                    { key: "signal", label: "Signal" },
                    { key: "observation", label: "Observation" },
                    { key: "baseline", label: "Baseline" },
                    { key: "threshold", label: "Threshold" },
                    { key: "severity", label: "Severity", render: (x) => <Badge value={x.severity} kind={x.severity} /> },
                    { key: "points", label: "Points", align: "right" },
                    { key: "evidence", label: "Evidence", render: (x) => x.evidence.map((e: string) => <button key={e} className="cite" onClick={() => cite(e)}>{e}</button>) },
                  ]}
                  empty="No indicator crossed its threshold."
                />
              </Card>
              <div className="grid2">
                <Card title="Risk Assessment">
                  <p>
                    <ScoreBadge score={ra.score} band={ra.band} /> threshold {ra.threshold} · config {ra.config_version}
                  </p>
                  <Bars data={(ra.contributors ?? []).map((c: any) => ({ label: c.signal_type, value: c.points, note: c.capped ? "(group cap)" : `(${c.weight}×${c.strength})` }))} />
                  <p className="muted small">{ra.method}</p>
                </Card>
                <Card title="Interpretation">
                  <Claims items={r.interpretation} onCite={cite} />
                </Card>
              </div>
              <Card title="Uncertainty">
                <div className="grid2">
                  {[
                    ["Missing data", unc.missing_data],
                    ["Conflicting evidence", unc.conflicting_evidence],
                    ["Weak signals", unc.weak_signals],
                    ["Assumptions", unc.assumptions],
                  ].map(([label, items]) => (
                    <div key={label as string}>
                      <h4>{label as string}</h4>
                      <Claims items={items as any[]} onCite={cite} />
                    </div>
                  ))}
                </div>
              </Card>
              <Card title="Recommended Investigation Actions (suggestions only)">
                <Claims items={r.recommended_actions} onCite={cite} />
              </Card>
              {r.validation && (
                <Card title="Claim validation">
                  <p>
                    {r.validation.total_claims} narrative claims checked, {r.validation.unsupported_claims} removed as unsupported; narrative by{" "}
                    <code>{r.validation.narrative_source}</code>.
                  </p>
                  {r.validation.rejected?.length > 0 && <JsonBlock value={r.validation.rejected} />}
                </Card>
              )}
            </>
          )}
          <Card title="Human Decision">
            {(d.decisions ?? []).length > 0 && (
              <Table rows={d.decisions} columns={[{ key: "decision", label: "Decision" }, { key: "decided_by", label: "By" }, { key: "rationale", label: "Rationale" }, { key: "created_at", label: "At" }]} />
            )}
            {["pending_review", "in_progress"].includes(d.status) ? (
              <div className="decision">
                <div className="row">
                  {DECISIONS.map(([v, label]) => (
                    <label key={v} className="radio">
                      <input type="radio" name="decision" checked={decision === v} onChange={() => setDecision(v)} /> {label}
                    </label>
                  ))}
                </div>
                <Field label="Rationale (required)">
                  <textarea rows={3} value={rationale} onChange={(e) => setRationale(e.target.value)} />
                </Field>
                <div className="row wrap">
                  <span className="muted">Report issues:</span>
                  {FAILURES.map((f) => (
                    <label key={f} className="radio">
                      <input type="checkbox" checked={fails.includes(f)} onChange={() => setFails(fails.includes(f) ? fails.filter((x) => x !== f) : [...fails, f])} /> {f.replace(/_/g, " ")}
                    </label>
                  ))}
                </div>
                <Field label="Report quality (1–5)">
                  <input type="number" min={1} max={5} value={quality} onChange={(e) => setQuality(Number(e.target.value))} style={{ width: 70 }} />
                </Field>
                <button className="primary" disabled={rationale.trim().length < 5} onClick={submitDecision}>
                  Record decision as {session.username}
                </button>
                {msg && <p>{msg}</p>}
              </div>
            ) : (
              <p className="muted">This investigation is {d.status}; no further decisions are accepted.</p>
            )}
          </Card>
        </>
      )}

      {tab === "Risk Signals" && (
        <Card title="Signals with evidence">
          {(evidence.data ?? [])
            .filter((e: any) => e.evidence_type.startsWith("risk_signal"))
            .map((e: any) => (
              <div key={e.ref} className="signal">
                <h4>
                  {e.ref} · {e.content.signal_type} <Badge value={e.content.severity} kind={e.content.severity} />{" "}
                  <span className="muted small">confidence {e.content.confidence}</span>
                </h4>
                <p>{e.content.description}</p>
                <p className="muted small">
                  observed {fmt(e.content.observed_value)} {e.content.unit} · baseline {fmt(e.content.baseline_value)} · threshold {fmt(e.content.threshold)}
                </p>
                <div className="row wrap">
                  {(e.content.evidence ?? []).slice(0, 12).map((x: any, i: number) => (
                    <span key={i} className="chip" title={x.note}>
                      {x.kind}:{x.id}
                    </span>
                  ))}
                </div>
              </div>
            ))}
        </Card>
      )}

      {tab === "Transactions" && (
        <Card title="Transactions referenced by risk signals">
          <Table
            rows={r.transaction_analysis?.key_transactions ?? []}
            columns={["transaction_id", "timestamp", "transaction_type", "channel", "amount", "currency", "amount_usd", "country", "receiver_account_id", "merchant_id", "external_counterparty", "device_id", "status"].map((k) => ({ key: k, label: k.replace(/_/g, " ") }))}
            empty="No transactions were referenced."
          />
        </Card>
      )}

      {tab === "Graph" && (
        <Card title="Relationships around the subject">
          {graph.data ? <GraphView nodes={graph.data.nodes ?? []} edges={graph.data.edges ?? []} center={graph.data.center} /> : <Status loading={graph.loading} error={graph.error} />}
          {r.graph_relationships?.cluster && (
            <p className="muted">
              Cluster: {r.graph_relationships.cluster.n_customers} customers, {r.graph_relationships.cluster.n_accounts} accounts, density {r.graph_relationships.cluster.density}; flagged:{" "}
              {(r.graph_relationships.cluster.flagged_customers ?? []).join(", ") || "none"}
            </p>
          )}
          {r.graph_relationships?.fund_flows && (
            <Table
              rows={r.graph_relationships.fund_flows.flows}
              columns={[
                { key: "path", label: "Fund flow path", render: (x) => x.path.join(" → ") },
                { key: "hops", label: "Hops" },
                { key: "min_edge_usd", label: "Min edge USD", align: "right" },
                { key: "last_ts", label: "Last" },
              ]}
            />
          )}
        </Card>
      )}

      {tab === "Documents" && (
        <Card title="Policy and procedure passages">
          {(r.document_evidence ?? []).map((doc: any) => (
            <div key={doc.ref} className="passage">
              <h4>
                {doc.ref} · {doc.title}
              </h4>
              <p className="muted small">
                {doc.section} · document {doc.document_id} · chunk {doc.chunk_id}
                {doc.page ? ` · page ${doc.page}` : ""} · retrieved for “{doc.retrieved_for}”
              </p>
              <blockquote>{doc.excerpt}</blockquote>
            </div>
          ))}
        </Card>
      )}

      {d.subject_type === "customer" && tab === "Evidence" && <Copilot customerId={d.subject_id} investigationId={id} />}

      {tab === "Evidence" && (
        <Card title={`Evidence (${evidence.data?.length ?? 0} items)`}>
          {(evidence.data ?? []).map((e: any) => (
            <details key={e.ref} open={focus === e.ref} className={focus === e.ref ? "evidence focus" : "evidence"}>
              <summary>
                <strong>{e.ref}</strong> {e.title} <span className="chip">{e.source_type}</span> <span className="muted small">confidence {e.confidence}</span>
              </summary>
              <p className="muted small">
                source {e.source_id} · type {e.evidence_type}
              </p>
              <JsonBlock value={e.content} />
            </details>
          ))}
          {focus && !evByRef.has(focus) && <div className="error">Evidence {focus} not found.</div>}
        </Card>
      )}

      {tab === "Agent Activity" && (
        <>
          {(episodes.data ?? []).map((ep: any) => (
            <Card key={ep.episode_id} title={`Agent run ${ep.episode_id} · ${ep.status} · ${ep.latency_ms ?? "?"} ms`}>
              <p>{ep.reasoning_summary}</p>
              <p className="muted small">Node trace: {(ep.final_output?.node_trace ?? []).join(" → ")}</p>
              <h4>Plan</h4>
              <Table rows={ep.plan ?? []} columns={[{ key: "step", label: "#" }, { key: "tool", label: "Tool" }, { key: "purpose", label: "Purpose" }]} />
              <h4>Tool calls</h4>
              <Table
                rows={ep.tool_calls ?? []}
                columns={[
                  { key: "node", label: "Node" },
                  { key: "tool", label: "Tool" },
                  { key: "ok", label: "OK", render: (x) => (x.ok ? <Badge value="ok" kind="low" /> : <Badge value={x.error_type} kind="high" />) },
                  { key: "latency_ms", label: "ms", align: "right" },
                  { key: "input", label: "Input", render: (x) => <code>{JSON.stringify(x.input)}</code> },
                ]}
              />
              <h4>Observations</h4>
              <ul>
                {(ep.observations ?? []).map((o: string, i: number) => (
                  <li key={i}>{o}</li>
                ))}
              </ul>
              {ep.human_feedback && (
                <>
                  <h4>Human feedback</h4>
                  <JsonBlock value={ep.human_feedback} />
                </>
              )}
            </Card>
          ))}
          {!episodes.data?.length && <Status loading={episodes.loading} error={episodes.error} />}
        </>
      )}

      {tab === "Timeline" && (
        <Card title="Investigation timeline">
          <ol className="timeline">
            {timeline.map((x, i) => (
              <li key={i}>
                <span className="muted">{fmt(x.t)}</span> {x.what}
              </li>
            ))}
          </ol>
        </Card>
      )}
    </div>
  );
}
