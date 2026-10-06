import { useState } from "react";
import { Session, api } from "../api";
import { Badge, Bars, Card, Kpi, Status, Table, errorText, fmt, useAsync } from "../components/ui";
import { formatHours, percent, ratio } from "../lib/workflow";

export default function MonitoringPage({ session }: { session: Session }) {
  const runs = useAsync(() => api.get<any[]>("/api/monitoring/runs?limit=20"), []);
  const dets = useAsync(() => api.get<any>("/api/monitoring/detectors"), []);
  const kpis = useAsync(() => api.get<any>("/api/monitoring/kpis"), []);
  const quality = useAsync(() => api.get<any>("/api/monitoring/quality"), []);
  const dq = useAsync(() => api.get<any>("/api/data-quality/summary"), []);
  const changes = useAsync(() => api.get<any[]>("/api/config/changes?limit=20"), []);
  const [lookback, setLookback] = useState("30");
  const [activeDays, setActiveDays] = useState("1");
  const [customers, setCustomers] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const isAdmin = session.role === "admin";

  const run = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const ids = customers.split(/[\s,]+/).map((s) => s.trim().toUpperCase()).filter(Boolean);
      const r: any = await api.post("/api/monitoring/run", {
        lookback_days: Number(lookback) || undefined,
        active_days: Number(activeDays) || undefined,
        customer_ids: ids.length ? ids : undefined,
      });
      setMsg(`Run ${r.run_id}: ${r.customers_evaluated} customers screened, ${r.alerts_created} alerts created, ${r.alerts_updated} updated (merged), ${r.alerts_suppressed} suppressed, ${r.duration_ms} ms.`);
      runs.reload();
    } catch (e) {
      setMsg(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <h2>Operations</h2>
      <div className="notice">
        Every figure on this page is computed from stored alerts, cases, runs and ingestion batches. Nothing is estimated. Where a value cannot be
        computed honestly (for example a false-positive <i>rate</i>, which needs verified true negatives), it is not shown.
      </div>
      <div className="grid2">
        <Card title="Alerts">
          {kpis.data && quality.data ? (
            <>
              <div className="kpis">
                <Kpi label="Open alerts" value={kpis.data.open_alerts} />
                <Kpi label="Decided" value={quality.data.overall.decided} />
                <Kpi label="Confirmation rate (of decided)" value={percent(quality.data.overall.confirmation_rate)} />
                <Kpi label="False-discovery rate (of decided)" value={percent(quality.data.overall.false_discovery_rate)} />
                <Kpi label="Closure rate" value={percent(quality.data.overall.closure_rate)} />
                <Kpi label="Median time to decision" value={formatHours(quality.data.time_to_decision_hours.median)} />
              </div>
              {quality.data.overall.low_sample && <p className="muted small">Fewer than 30 decided alerts: treat these rates as indicative only.</p>}
              <p className="muted small">Not computed: {Object.entries(quality.data.not_computed).map(([k, v]) => `${k} (${v})`).join(" ")}</p>
            </>
          ) : (
            <Status loading={kpis.loading || quality.loading} error={kpis.error ?? quality.error} />
          )}
        </Card>
        <Card title="Alerts by triage priority (all alerts in the store)">
          {quality.data ? (
            <Table
              rows={Object.entries(quality.data.by_priority).map(([k, v]: [string, any]) => ({ priority: k, ...v }))}
              empty="No alerts yet."
              columns={[
                { key: "priority", label: "Priority", render: (r) => <Badge value={r.priority} kind={r.priority === "UNSCORED" ? undefined : r.priority === "CRITICAL" ? "critical" : r.priority === "HIGH" ? "high" : r.priority === "MEDIUM" ? "medium" : "low"} /> },
                { key: "alerts", label: "Alerts", align: "right" },
                { key: "open", label: "Open", align: "right" },
                { key: "decided", label: "Decided", align: "right" },
                { key: "confirmed", label: "Confirmed", align: "right" },
                { key: "confirmation_rate", label: "Confirmation", align: "right", render: (r) => percent(r.confirmation_rate) },
              ]}
            />
          ) : (
            <Status loading={quality.loading} error={quality.error} />
          )}
        </Card>
      </div>
      <Card title="Data quality" actions={<a href="#/data-quality">details and rejected records →</a>}>
        {dq.data ? (
          <div className="kpis">
            <Kpi label="Received" value={dq.data.totals.received.toLocaleString()} />
            <Kpi label="Processed" value={dq.data.totals.processed.toLocaleString()} />
            <Kpi label="Rejected" value={dq.data.totals.rejected.toLocaleString()} />
            <Kpi label="Coverage" value={percent(dq.data.totals.coverage)} />
            <Kpi label="Processing success" value={percent(dq.data.totals.processing_success)} />
            <Kpi label="Rejection share" value={ratio(dq.data.totals.rejected, dq.data.totals.received)} />
          </div>
        ) : (
          <Status loading={dq.loading} error={dq.error} />
        )}
      </Card>
      {kpis.data && (
        <Card title="Alert volume by detector">
          <Bars data={Object.entries(kpis.data.alerts_by_detector as Record<string, number>).map(([k, v]) => ({ label: k, value: v }))} />
        </Card>
      )}
      <Card title="Configuration changes (append-only log)">
        {changes.data ? (
          <Table
            rows={changes.data}
            empty="No changes recorded since the baseline."
            columns={[
              { key: "ts", label: "When", render: (r) => fmt(r.ts) },
              { key: "config_name", label: "Config" },
              { key: "path", label: "Setting" },
              { key: "old_value", label: "Old" },
              { key: "new_value", label: "New" },
              { key: "changed_by", label: "By" },
              { key: "source", label: "Source" },
              { key: "reason", label: "Reason" },
            ]}
          />
        ) : (
          <Status loading={changes.loading} error={changes.error} />
        )}
      </Card>
      <h2>Transaction monitoring</h2>
      <div className="notice">
        A monitoring run screens customers with recent activity using the deterministic risk detectors and creates or updates alerts. Re-running
        over the same data does not duplicate alerts: an unresolved alert for the same customer and detector is merged instead.
      </div>
      <Card title="Run monitoring">
        {isAdmin ? (
          <div className="decision">
            <div className="row wrap">
              <label className="field"><span>Lookback (days)</span><input value={lookback} onChange={(e) => setLookback(e.target.value)} style={{ width: 90 }} /></label>
              <label className="field"><span>Screen customers active in the last (days)</span><input value={activeDays} onChange={(e) => setActiveDays(e.target.value)} style={{ width: 90 }} /></label>
              <label className="field" style={{ flex: 1 }}><span>Only these customers (optional, comma-separated)</span><input value={customers} onChange={(e) => setCustomers(e.target.value)} placeholder="CUST-10686, CUST-17781" /></label>
              <button className="primary" disabled={busy} onClick={run}>{busy ? "Running…" : "Run monitoring"}</button>
            </div>
            <p className="muted small">Runs are synchronous; for the whole customer base use the CLI (python -m app.monitoring.run) instead.</p>
          </div>
        ) : (
          <p className="muted">Only administrators can start monitoring runs.</p>
        )}
        {msg && <div className="notice" style={{ marginTop: 8 }}>{msg}</div>}
      </Card>
      <Card title="Recent runs">
        {runs.data ? (
          <Table
            rows={runs.data}
            empty="No runs yet."
            columns={[
              { key: "run_id", label: "Run" },
              { key: "started_at", label: "Started", render: (r) => fmt(r.started_at) },
              { key: "mode", label: "Mode" },
              { key: "customers_evaluated", label: "Customers", align: "right" },
              { key: "transactions_in_scope", label: "Txns in scope", align: "right" },
              { key: "alerts_created", label: "Created", align: "right" },
              { key: "alerts_updated", label: "Merged", align: "right" },
              { key: "alerts_suppressed", label: "Suppressed", align: "right" },
              { key: "errors", label: "Errors", align: "right" },
              { key: "duration_ms", label: "ms", align: "right" },
              { key: "config_version", label: "Risk config" },
              { key: "triggered_by", label: "By" },
            ]}
          />
        ) : (
          <Status loading={runs.loading} error={runs.error} />
        )}
      </Card>
      <Card title="Detectors">
        {dets.data ? (
          <Table
            rows={dets.data.detectors}
            columns={[
              { key: "detector_id", label: "Detector" },
              { key: "category", label: "Category" },
              { key: "description", label: "What it looks for" },
              { key: "weight", label: "Weight", align: "right" },
              { key: "enabled", label: "Enabled" },
              { key: "alert_tier", label: "Alert policy", render: (r) => (r.alert_tier === "standalone" ? <Badge value="alerts on its own" kind="high" /> : r.alert_tier === "supporting" ? <Badge value="when customer score is flagged" kind="medium" /> : <span className="muted">context only</span>) },
            ]}
          />
        ) : (
          <Status loading={dets.loading} error={dets.error} />
        )}
        <p className="muted small">Weights and thresholds are engineering defaults for a synthetic-data prototype, not calibrated on real data.</p>
      </Card>
    </div>
  );
}
