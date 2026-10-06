import { useState } from "react";
import { Session, api } from "../api";
import { Badge, Card, ScoreBadge, Status, Table, errorText, fmt, useAsync } from "../components/ui";
import { ALERT_STATUSES, AlertFilters, PRIORITIES, buildAlertQuery, filtersFromQuery, nextAlertStatuses, pageCount, priorityKind } from "../lib/workflow";
import { go } from "../router";

const SEVERITIES = ["critical", "high", "medium", "low"];
const SORTS: [string, string][] = [
  ["triage_score", "Triage score"],
  ["triggered_at", "Triggered"],
  ["risk_score", "Risk score"],
  ["severity_rank", "Severity"],
  ["status", "Status"],
  ["customer_id", "Customer"],
  ["detector_id", "Detector"],
];

function toggle(list: string[] | undefined, v: string): string[] {
  const cur = list ?? [];
  return cur.includes(v) ? cur.filter((x) => x !== v) : [...cur, v];
}

export default function AlertsPage({ session, initial }: { session: Session; initial?: Record<string, string> }) {
  const [f, setF] = useState<AlertFilters>({
    status: ["NEW", "TRIAGED", "INVESTIGATING", "ESCALATED"], sort: "triage_score", order: "desc", page: 1, pageSize: 25,
    ...filtersFromQuery(initial ?? {}),
  });
  const [draftQ, setDraftQ] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const query = buildAlertQuery(f);
  const list = useAsync(() => api.get<any>(`/api/monitoring/alerts${query}`), [query]);
  const dets = useAsync(() => api.get<any>("/api/monitoring/detectors"), []);
  const kp = useAsync(() => api.get<any>("/api/monitoring/kpis"), [query]);
  const set = (patch: Partial<AlertFilters>) => setF((p) => ({ ...p, ...patch, page: patch.page ?? 1 }));

  const act = async (fn: () => Promise<unknown>) => {
    setMsg(null);
    try {
      await fn();
      list.reload();
      kp.reload();
    } catch (e) {
      setMsg(errorText(e));
    }
  };
  const items: any[] = list.data?.items ?? [];
  const total: number = list.data?.total ?? 0;
  const pages = pageCount(total, f.pageSize ?? 25);

  return (
    <div className="page">
      <h2>Alert queue</h2>
      <div className="notice">
        FIRA monitoring alerts are created automatically when a configured detector triggers during a monitoring run. They are separate from
        the seeded synthetic legacy alerts shown on the dashboard.
      </div>
      {kp.data && (
        <div className="kpis">
          <div className="kpi"><div className="kpi-value">{kp.data.alerts_generated}</div><div className="kpi-label">Alerts generated</div></div>
          <div className="kpi"><div className="kpi-value">{kp.data.open_alerts}</div><div className="kpi-label">Open alerts</div></div>
          <div className="kpi"><div className="kpi-value">{kp.data.high_risk_alerts}</div><div className="kpi-label">High / critical (open)</div></div>
        </div>
      )}
      <Card title="Filters">
        <div className="row wrap">
          {ALERT_STATUSES.map((s) => (
            <label key={s} className="radio">
              <input type="checkbox" checked={f.status?.includes(s) ?? false} onChange={() => set({ status: toggle(f.status, s) })} /> {s}
            </label>
          ))}
        </div>
        <div className="row wrap" style={{ marginTop: 8 }}>
          <span className="muted">Triage priority:</span>
          {PRIORITIES.map((p) => (
            <label key={p} className="radio">
              <input type="checkbox" checked={f.priority?.includes(p) ?? false} onChange={() => set({ priority: toggle(f.priority, p) })} /> {p}
            </label>
          ))}
        </div>
        <div className="row wrap" style={{ marginTop: 8 }}>
          <span className="muted">Severity:</span>
          {SEVERITIES.map((s) => (
            <label key={s} className="radio">
              <input type="checkbox" checked={f.severity?.includes(s) ?? false} onChange={() => set({ severity: toggle(f.severity, s) })} /> {s}
            </label>
          ))}
          <select value={f.detector ?? ""} onChange={(e) => set({ detector: e.target.value })} aria-label="detector">
            <option value="">all detectors</option>
            {(dets.data?.detectors ?? []).filter((d: any) => d.creates_alerts).map((d: any) => (
              <option key={d.detector_id} value={d.detector_id}>{d.name}</option>
            ))}
          </select>
          <input placeholder="min risk" style={{ width: 80 }} value={f.minRisk ?? ""} onChange={(e) => set({ minRisk: e.target.value })} />
          <input placeholder="max risk" style={{ width: 80 }} value={f.maxRisk ?? ""} onChange={(e) => set({ maxRisk: e.target.value })} />
          <input placeholder="CUST-…" style={{ width: 120 }} value={f.customer ?? ""} onChange={(e) => set({ customer: e.target.value })} />
          <select value={f.assignedTo ?? ""} onChange={(e) => set({ assignedTo: e.target.value })} aria-label="assignee">
            <option value="">any investigator</option>
            <option value="me">assigned to me</option>
            <option value="unassigned">unassigned</option>
          </select>
        </div>
        <div className="row wrap" style={{ marginTop: 8 }}>
          <label className="field"><span>From</span><input type="date" value={f.dateFrom ?? ""} onChange={(e) => set({ dateFrom: e.target.value })} /></label>
          <label className="field"><span>To</span><input type="date" value={f.dateTo ?? ""} onChange={(e) => set({ dateTo: e.target.value ? `${e.target.value}T23:59:59` : "" })} /></label>
          <form className="row" onSubmit={(e) => { e.preventDefault(); set({ q: draftQ }); }}>
            <input placeholder="search id, customer, text" value={draftQ} onChange={(e) => setDraftQ(e.target.value)} style={{ width: 220 }} />
            <button>Search</button>
          </form>
          <select value={f.sort} onChange={(e) => set({ sort: e.target.value })} aria-label="sort">
            {SORTS.map(([k, l]) => <option key={k} value={k}>sort: {l}</option>)}
          </select>
          <select value={f.order} onChange={(e) => set({ order: e.target.value as "asc" | "desc" })} aria-label="order">
            <option value="desc">descending</option>
            <option value="asc">ascending</option>
          </select>
        </div>
      </Card>
      {msg && <div className="error">{msg}</div>}
      <Card title={`${total.toLocaleString()} alert(s)`}>
        {list.data ? (
          <Table
            rows={items}
            empty="No alerts match. Run monitoring from the Monitoring page (admin) or relax the filters."
            columns={[
              { key: "alert_id", label: "Alert", render: (r) => <a href={`#/alerts/${r.alert_id}`}>{r.alert_id}</a> },
              { key: "triage_priority", label: "Priority", render: (r) => (r.triage_priority ? <span title="heuristic triage score (0-100), not a probability"><Badge value={`${r.triage_priority} · ${r.triage_score}`} kind={priorityKind(r.triage_priority)} /></span> : <span className="muted">unscored</span>) },
              { key: "severity", label: "Severity", render: (r) => <Badge value={r.severity} kind={r.severity} /> },
              { key: "risk_score", label: "Risk", render: (r) => <ScoreBadge score={r.risk_score} /> },
              { key: "detector_id", label: "Detector" },
              { key: "customer_id", label: "Customer", render: (r) => <a href={`#/customers/${r.customer_id}`}>{r.customer_id}</a> },
              { key: "description", label: "Why", render: (r) => <span title={r.description}>{String(r.description).slice(0, 90)}{String(r.description).length > 90 ? "…" : ""}</span> },
              { key: "status", label: "Status", render: (r) => <Badge value={r.status} /> },
              { key: "occurrence_count", label: "×", align: "right" },
              { key: "assigned_to", label: "Assigned" },
              { key: "triggered_at", label: "Triggered", render: (r) => fmt(r.triggered_at) },
              {
                key: "actions", label: "",
                render: (r) => (
                  <span className="row">
                    {!r.assigned_to && r.status !== "RESOLVED" && (
                      <button onClick={() => act(() => api.post(`/api/monitoring/alerts/${r.alert_id}/assign`, { assignee: `U-${session.username}` }))}>Assign to me</button>
                    )}
                    {nextAlertStatuses(r.status).includes("TRIAGED") && (
                      <button onClick={() => act(() => api.post(`/api/monitoring/alerts/${r.alert_id}/transition`, { status: "TRIAGED" }))}>Triage</button>
                    )}
                    {r.status !== "RESOLVED" && !r.case_id && (
                      <button onClick={async () => { try { const c: any = await api.post(`/api/monitoring/alerts/${r.alert_id}/case`, {}); go(`/cases/${c.case.case_id}`); } catch (e) { setMsg(errorText(e)); } }}>Create case</button>
                    )}
                  </span>
                ),
              },
            ]}
          />
        ) : (
          <Status loading={list.loading} error={list.error} />
        )}
        <div className="row" style={{ marginTop: 10 }}>
          <button disabled={(f.page ?? 1) <= 1} onClick={() => setF((p) => ({ ...p, page: (p.page ?? 1) - 1 }))}>Previous</button>
          <span className="muted">Page {f.page ?? 1} of {pages}</span>
          <button disabled={(f.page ?? 1) >= pages} onClick={() => setF((p) => ({ ...p, page: (p.page ?? 1) + 1 }))}>Next</button>
        </div>
      </Card>
    </div>
  );
}
