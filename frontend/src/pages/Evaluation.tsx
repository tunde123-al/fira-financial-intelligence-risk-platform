import { useState } from "react";
import { Session, api } from "../api";
import { Badge, Bars, Card, JsonBlock, Status, Table, errorText, fmt, useAsync } from "../components/ui";

export default function EvaluationPage({ session }: { session: Session }) {
  const runs = useAsync(() => api.get<any[]>("/api/evaluation/runs"), []);
  const failures = useAsync(() => api.get("/api/evaluation/failures"), []);
  const versions = useAsync(() => api.get("/api/config/versions"), []);
  const [sel, setSel] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const act = async (label: string, fn: () => Promise<any>) => {
    setBusy(label);
    setMsg(null);
    try {
      const r = await fn();
      setMsg(`${label}: ${r?.status ?? "done"}`);
      runs.reload();
      versions.reload();
      failures.reload();
    } catch (e) {
      setMsg(`${label}: ${errorText(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const run = runs.data?.[sel];
  const m = run?.metrics ?? {};
  const risk = m.risk;
  const retr = m.retrieval;
  const agent = m.agent;
  return (
    <div className="page">
      <div className="page-head">
        <h2>Evaluation</h2>
        {session.role === "admin" && (
          <button className="primary" disabled={!!busy} onClick={() => act("Evaluation run", () => api.post("/api/evaluation/run", { kinds: ["risk", "retrieval", "agent"], per_scenario: 10, agent_sample: 2 }))}>
            {busy === "Evaluation run" ? "Running…" : "Run benchmarks"}
          </button>
        )}
      </div>
      {msg && <div className="notice">{msg}</div>}
      {!runs.data ? (
        <Status loading={runs.loading} error={runs.error} />
      ) : !runs.data.length ? (
        <Card>No evaluation runs yet. Run `make evaluate` or use the button above (admin).</Card>
      ) : (
        <>
          <div className="row">
            <span className="muted">Run:</span>
            <select value={sel} onChange={(e) => setSel(Number(e.target.value))}>
              {runs.data.map((r: any, i: number) => (
                <option key={r.run_id} value={i}>
                  {r.run_id} · {fmt(r.created_at)} · {r.kind} · config {r.config_version}
                </option>
              ))}
            </select>
          </div>
          {risk && (
            <Card title={`Risk detection (threshold ${risk.threshold}, n=${risk.n})`}>
              <Table
                rows={[risk.overall]}
                columns={["precision", "recall", "f1", "false_positive_rate", "false_negative_rate", "auc", "tp", "fp", "fn", "tn"].map((k) => ({ key: k, label: k.replace(/_/g, " "), align: "right" as const }))}
              />
              <h4>By scenario</h4>
              <Table
                rows={Object.entries(risk.per_scenario ?? {}).map(([k, v]: any) => ({ scenario: k, ...v }))}
                columns={[
                  { key: "scenario", label: "Scenario" },
                  { key: "suspicious", label: "Ground truth", render: (r) => (r.suspicious ? <Badge value="suspicious" kind="high" /> : <Badge value="legitimate" kind="low" />) },
                  { key: "n", label: "n", align: "right" },
                  { key: "flag_rate", label: "Flag rate", align: "right" },
                  { key: "mean_score", label: "Mean score", align: "right" },
                  { key: "expected_signal_recall", label: "Expected-signal recall", align: "right" },
                ]}
              />
              <h4>Signal precision (share of firings on suspicious subjects)</h4>
              <Bars data={Object.entries(risk.signals ?? {}).map(([k, v]: any) => ({ label: k, value: v.precision, note: `(${v.fired} fired)` }))} max={1} />
            </Card>
          )}
          {retr && (
            <Card title="Retrieval">
              <Table
                rows={Object.entries(retr).map(([mode, v]: any) => ({ mode, ...v }))}
                columns={["mode", "n_queries", "p@1", "p@3", "r@3", "r@5", "mrr"].map((k) => ({ key: k, label: k }))}
              />
            </Card>
          )}
          {agent && (
            <Card title={`Agent, RAG and system (n=${agent.n}, engine ${agent.engine})`}>
              <div className="grid2">
                {["agent", "rag", "system"].map((g) => (
                  <Table key={g} rows={Object.entries(agent[g] ?? {}).map(([k, v]) => ({ metric: `${g}.${k}`, value: v }))} columns={[{ key: "metric", label: "Metric" }, { key: "value", label: "Value", align: "right" }]} />
                ))}
              </div>
            </Card>
          )}
        </>
      )}
      <Card title="Failure patterns from analyst feedback">
        {failures.data ? (
          <>
            <p>{failures.data.n_decisions} decisions analysed.</p>
            <Bars data={Object.entries(failures.data.category_counts ?? {}).map(([k, v]) => ({ label: k, value: Number(v) }))} />
            <h4>Per signal (reviewed outcomes)</h4>
            <Table rows={Object.entries(failures.data.per_signal ?? {}).map(([k, v]: any) => ({ signal: k, ...v }))} columns={[{ key: "signal", label: "Signal" }, { key: "tp", label: "Confirmed", align: "right" }, { key: "fp", label: "False positive", align: "right" }, { key: "fn", label: "Missed", align: "right" }]} empty="No feedback yet." />
          </>
        ) : (
          <Status loading={failures.loading} error={failures.error} />
        )}
      </Card>
      <Card
        title="Risk configuration versions (controlled improvement)"
        actions={
          <button disabled={!!busy} onClick={() => act("Proposal", () => api.post("/api/config/proposals", { per_scenario: 10 }))}>
            {busy === "Proposal" ? "Evaluating…" : "Propose from feedback"}
          </button>
        }
      >
        {versions.data ? (
          <>
            <p>
              Active: <code>{versions.data.active.version}</code> ({versions.data.active.fingerprint})
            </p>
            <Table
              rows={versions.data.versions}
              columns={[
                { key: "version_id", label: "Version" },
                { key: "status", label: "Status", render: (r) => <Badge value={r.status} kind={r.status === "rejected" ? "high" : r.status === "active" ? "low" : undefined} /> },
                { key: "created_by", label: "Proposed by" },
                { key: "rationale", label: "Rationale" },
                { key: "regression", label: "Regression", render: (r) => <code>{JSON.stringify(r.evaluation?.regression?.checks ?? {})}</code> },
                { key: "approved_by", label: "Approved by" },
                {
                  key: "act",
                  label: "",
                  render: (r) =>
                    session.role === "admin" && r.status === "validated" ? (
                      <button onClick={() => act("Approval", () => api.post(`/api/config/versions/${r.version_id}/approve`))}>Approve</button>
                    ) : null,
                },
              ]}
              empty="No proposals yet."
            />
            <details>
              <summary>Active configuration (YAML)</summary>
              <pre className="json">{versions.data.active.yaml}</pre>
            </details>
          </>
        ) : (
          <Status loading={versions.loading} error={versions.error} />
        )}
      </Card>
      {run && (
        <details>
          <summary>Raw run record</summary>
          <JsonBlock value={run} />
        </details>
      )}
    </div>
  );
}
