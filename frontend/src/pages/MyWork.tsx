import { Session, api } from "../api";
import { Badge, Card, Kpi, Status, Table, fmt, useAsync } from "../components/ui";
import { priorityKind } from "../lib/workflow";

function AlertTable({ rows, empty }: { rows: any[]; empty: string }) {
  return (
    <Table
      rows={rows}
      empty={empty}
      columns={[
        { key: "alert_id", label: "Alert", render: (r) => <a href={`#/alerts/${r.alert_id}`}>{r.alert_id}</a> },
        { key: "triage_priority", label: "Priority", render: (r) => (r.triage_priority ? <Badge value={`${r.triage_priority} · ${r.triage_score}`} kind={priorityKind(r.triage_priority)} /> : <span className="muted">unscored</span>) },
        { key: "severity", label: "Severity", render: (r) => <Badge value={r.severity} kind={r.severity} /> },
        { key: "detector_id", label: "Detector" },
        { key: "customer_id", label: "Customer", render: (r) => <a href={`#/customers/${r.customer_id}`}>{r.customer_id}</a> },
        { key: "status", label: "Status" },
        { key: "triggered_at", label: "Raised", render: (r) => fmt(r.triggered_at) },
      ]}
    />
  );
}

export default function MyWorkPage({ session }: { session: Session }) {
  const w = useAsync(() => api.get<any>("/api/monitoring/my-work"), []);
  if (!w.data) return <Status loading={w.loading} error={w.error} />;
  const c = w.data.counts;
  const q = (query: string) => `#/alerts?assigned=me&${query}`;
  return (
    <div className="page">
      <h2>My work — {session.username}</h2>
      <div className="notice">
        Your open alerts and cases, ordered by triage score. Overdue uses review-time targets from configuration ({Object.entries(w.data.overdue_targets_hours).map(([k, v]) => `${k} ${v} h`).join(", ")}); {w.data.overdue_note}.
        Recent means the last {w.data.recent_days} days.
      </div>
      <div className="kpis">
        <Kpi label="My open alerts" value={<a href={q("sort=triage_score")}>{c.open}</a>} />
        <Kpi label="High priority (CRITICAL/HIGH)" value={<a href={q("priority=CRITICAL,HIGH&sort=triage_score")}>{c.high_priority}</a>} />
        <Kpi label="Overdue" value={c.overdue} />
        <Kpi label="Escalated" value={c.escalated} />
        <Kpi label="Confirmed (recent)" value={c.confirmed_recent} />
        <Kpi label="Cleared / false positive (recent)" value={c.cleared_recent} />
        <Kpi label="My open cases" value={c.open_cases} />
      </div>
      <Card title="Overdue alerts"><AlertTable rows={w.data.overdue} empty="Nothing overdue." /></Card>
      <Card title="High-priority alerts"><AlertTable rows={w.data.high_priority} empty="No CRITICAL or HIGH alerts assigned to you." /></Card>
      <Card title="All my open alerts (by triage score)"><AlertTable rows={w.data.open_alerts} empty="No open alerts assigned to you. Assign alerts to yourself from the alert queue." /></Card>
      <div className="grid2">
        <Card title="Recently escalated"><AlertTable rows={w.data.recently_escalated} empty="None." /></Card>
        <Card title="Recently confirmed suspicious"><AlertTable rows={w.data.recently_confirmed} empty="None." /></Card>
      </div>
      <Card title="Recently cleared / false positive"><AlertTable rows={w.data.recently_cleared} empty="None." /></Card>
      <Card title="My open cases">
        <Table
          rows={w.data.open_cases}
          empty="No open cases assigned to you."
          columns={[
            { key: "case_number", label: "Case", render: (r) => <a href={`#/cases/${r.case_id}`}>{r.case_number}</a> },
            { key: "customer_id", label: "Customer", render: (r) => <a href={`#/customers/${r.customer_id}`}>{r.customer_id}</a> },
            { key: "priority", label: "Priority", render: (r) => <Badge value={r.priority} kind={r.priority} /> },
            { key: "status", label: "Status" },
            { key: "title", label: "Title" },
            { key: "opened_at", label: "Opened", render: (r) => fmt(r.opened_at) },
          ]}
        />
      </Card>
    </div>
  );
}
