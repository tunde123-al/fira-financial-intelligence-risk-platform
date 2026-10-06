import { api } from "../api";
import { Bars, Card, Kpi, ScoreBadge, Status, Table, Badge, useAsync, fmt } from "../components/ui";
import { entityHref } from "../router";
import { formatHours, percent } from "../lib/workflow";

// The vector store counts chunks, not documents; label the two numbers accurately.
const HEALTH_LABELS: Record<string, string> = {
  indexed_chunks: "indexed chunks",
  source_documents: "source documents",
};

export default function Dashboard() {
  const dq = useAsync(() => api.get<any>("/api/data-quality/dataset"), []);
  const { data, error, loading } = useAsync(() => api.get("/api/dashboard"), []);
  const health = useAsync(() => api.get("/health/ready").catch((e) => ({ status: "degraded", checks: { error: String(e) } })), []);
  if (!data) return <Status loading={loading} error={error} />;
  const sev = data.open_alerts_by_severity ?? {};
  const byStatus = data.investigations_by_status ?? {};
  return (
    <div className="page">
      <h2>Dashboard</h2>
      <p className="muted">Data as of {fmt(data.as_of)} (synthetic dataset)</p>
      <div className="kpis">
        <Kpi label="Customers" value={data.counts.customers.toLocaleString()} />
        <Kpi label="Accounts" value={data.counts.accounts.toLocaleString()} />
        <Kpi label="Transactions" value={data.counts.transactions.toLocaleString()} />
        <Kpi label="Open legacy alerts (seeded)" value={Object.values(sev).reduce((a: number, b: any) => a + Number(b), 0).toLocaleString()} />
        <Kpi label="Data quality (stored transactions)" value={dq.data ? (dq.data.quality_score === null ? "n/a" : `${dq.data.quality_score}% valid`) : dq.error ? "unavailable" : "…"} />
        <Kpi label="Active investigations" value={((byStatus.in_progress ?? 0) + (byStatus.pending_review ?? 0) + (byStatus.open ?? 0)).toLocaleString()} />
      </div>
      {data.monitoring && (
        <>
          <h3>Transaction monitoring (FIRA alerts and cases)</h3>
          <div className="kpis">
            <Kpi label="Transactions screened (latest run)" value={data.monitoring.transactions_monitored_last_run?.toLocaleString() ?? "—"} />
            <Kpi label="Alerts generated" value={data.monitoring.alerts_generated.toLocaleString()} />
            <Kpi label="Open alerts" value={data.monitoring.open_alerts.toLocaleString()} />
            <Kpi label="High / critical open alerts" value={data.monitoring.high_risk_alerts.toLocaleString()} />
            <Kpi label="Cases under investigation" value={data.monitoring.cases_under_investigation.toLocaleString()} />
            <Kpi label="Cases escalated" value={data.monitoring.cases_escalated.toLocaleString()} />
            <Kpi label="Cases resolved" value={data.monitoring.cases_resolved.toLocaleString()} />
            <Kpi label="False-discovery rate (decided alerts)" value={percent(data.monitoring.false_discovery_rate)} />
            <Kpi label="Detection time per customer" value={data.monitoring.avg_detection_ms_per_customer === null ? "—" : `${data.monitoring.avg_detection_ms_per_customer} ms`} />
            <Kpi label="Avg investigation duration" value={formatHours(data.monitoring.avg_investigation_hours)} />
          </div>
          <p className="muted small">
            Computed from stored alerts, cases and runs; shows "—" until there is data. Detection time is processing time per screened customer, measured
            in this deployment. These alerts are separate from the seeded legacy alerts below.
          </p>
        </>
      )}
      <div className="grid2">
        <Card title="Risk distribution (investigated subjects)">
          <Bars data={(data.risk_distribution ?? []).map((b: any) => ({ label: b.bucket, value: b.count }))} />
        </Card>
        <Card title="Open alerts by severity (seeded synthetic legacy alerts)">
          <Bars data={["critical", "high", "medium", "low"].map((s) => ({ label: s, value: Number(sev[s] ?? 0) }))} />
        </Card>
        <Card title="Investigations by status">
          <Bars data={Object.entries(byStatus).map(([k, v]) => ({ label: k, value: Number(v) }))} />
        </Card>
        <Card title="System health">
          {health.data ? (
            <Table
              rows={Object.entries({ ...(health.data.checks ?? {}), risk_config: data.system?.risk_config }).map(([k, v]) => ({ k: HEALTH_LABELS[k] ?? k, v }))}
              columns={[
                { key: "k", label: "Component" },
                { key: "v", label: "Status", render: (r) => (r.v === "ok" ? <Badge value="ok" kind="low" /> : fmt(r.v)) },
              ]}
            />
          ) : (
            <Status loading={health.loading} error={health.error} />
          )}
        </Card>
      </div>
      <Card title="Recent investigations">
        <Table
          rows={data.recent_investigations ?? []}
          columns={[
            { key: "investigation_id", label: "Investigation", render: (r) => <a href={entityHref("investigation", r.investigation_id)}>{r.investigation_id}</a> },
            { key: "subject_id", label: "Subject" },
            { key: "status", label: "Status", render: (r) => <Badge value={r.status} /> },
            { key: "risk_score", label: "Risk", render: (r) => <ScoreBadge score={r.risk_score} /> },
            { key: "conclusion", label: "Conclusion" },
            { key: "created_at", label: "Created" },
          ]}
        />
      </Card>
      <Card title="Open alerts (latest; seeded synthetic legacy alerts, not created by the FIRA risk engine)">
        <Table
          rows={data.open_alerts ?? []}
          columns={[
            { key: "alert_id", label: "Alert" },
            { key: "entity_id", label: "Entity", render: (r) => <a href={entityHref(r.entity_type, r.entity_id)}>{r.entity_id}</a> },
            { key: "alert_type", label: "Type" },
            { key: "severity", label: "Severity", render: (r) => <Badge value={r.severity} kind={r.severity} /> },
            { key: "created_at", label: "Created" },
          ]}
        />
      </Card>
    </div>
  );
}
