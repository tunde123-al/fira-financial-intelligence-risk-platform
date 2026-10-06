import { useState } from "react";
import { Session, api } from "../api";
import { Badge, Bars, Card, ScoreBadge, Status, Table, errorText, fmt, useAsync } from "../components/ui";
import { RESOLUTIONS, nextAlertStatuses, priorityKind, reasonOk } from "../lib/workflow";
import { go } from "../router";

export default function AlertDetailPage({ id, session }: { id: string; session: Session }) {
  const d = useAsync(() => api.get<any>(`/api/monitoring/alerts/${id}`), [id]);
  const [reason, setReason] = useState("");
  const [resolution, setResolution] = useState<string>("FALSE_POSITIVE");
  const [msg, setMsg] = useState<string | null>(null);
  if (!d.data) return <Status loading={d.loading} error={d.error} />;
  const a = d.data.alert;
  const ex = a.explanation ?? {};
  const next = nextAlertStatuses(a.status);

  const run = async (fn: () => Promise<unknown>, ok?: string) => {
    setMsg(null);
    try {
      await fn();
      if (ok) setMsg(ok);
      d.reload();
    } catch (e) {
      setMsg(errorText(e));
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h2>
            {a.alert_id} <Badge value={a.status} /> <Badge value={a.severity} kind={a.severity} />
            {a.resolution && <Badge value={a.resolution} />}
            {a.triage_priority && <Badge value={`${a.triage_priority} priority`} kind={priorityKind(a.triage_priority)} />}
          </h2>
          <p className="muted">
            {ex.detector ?? a.detector_id} · customer <a href={`#/customers/${a.customer_id}`}>{a.customer_id}</a>
            {a.account_id && <> · account <a href={`#/graph?kind=account&id=${a.account_id}`}>{a.account_id}</a></>} · raised {fmt(a.triggered_at)}
            {a.occurrence_count > 1 && <> · seen {a.occurrence_count}× (last {fmt(a.last_seen_at)})</>}
          </p>
        </div>
        <div className="row">
          <ScoreBadge score={a.risk_score} />
          {d.data.case ? (
            <a href={`#/cases/${d.data.case.case_id}`}>Case {d.data.case.case_number}</a>
          ) : (
            a.status !== "RESOLVED" && (
              <button className="primary" onClick={() => run(async () => { const c: any = await api.post(`/api/monitoring/alerts/${id}/case`, {}); go(`/cases/${c.case.case_id}`); })}>
                Create case
              </button>
            )
          )}
        </div>
      </div>
      {msg && <div className="notice">{msg}</div>}
      <div className="grid2">
        <Card title="Why this alert was raised">
          <p>{ex.summary ?? a.description}</p>
          <dl className="dl">
            <dt>Detector</dt><dd>{a.detector_id} ({a.category})</dd>
            <dt>Observed</dt><dd>{fmt(ex.observed)} {ex.unit}</dd>
            <dt>Baseline</dt><dd>{fmt(ex.baseline)}</dd>
            <dt>Threshold</dt><dd>{fmt(ex.threshold)}</dd>
            <dt>Detector points</dt><dd>{fmt(a.risk_contribution)}</dd>
            <dt>Customer risk score</dt><dd>{fmt(a.risk_score)}</dd>
            <dt>Window</dt><dd>{fmt(a.window_start)} → {fmt(a.window_end)}</dd>
          </dl>
          <p className="muted small">Rule-based and deterministic. No model decided that this alert exists.</p>
        </Card>
        <Card title="Work this alert">
          <dl className="dl">
            <dt>Assigned to</dt><dd>{fmt(a.assigned_to)}</dd>
          </dl>
          {a.status !== "RESOLVED" && (
            <div className="decision">
              {!a.assigned_to && <div><button onClick={() => run(() => api.post(`/api/monitoring/alerts/${id}/assign`, { assignee: `U-${session.username}` }))}>Assign to me</button></div>}
              <label className="field">
                <span>Reason (required for escalation and resolution, at least 5 characters)</span>
                <textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} />
              </label>
              <div className="row wrap">
                {next.filter((s) => s !== "RESOLVED").map((s) => (
                  <button key={s} disabled={s === "ESCALATED" && !reasonOk(reason)} onClick={() => run(() => api.post(`/api/monitoring/alerts/${id}/transition`, { status: s, reason: reason || undefined }))}>
                    Move to {s}
                  </button>
                ))}
              </div>
              <div className="row wrap">
                <select value={resolution} onChange={(e) => setResolution(e.target.value)} aria-label="resolution">
                  {RESOLUTIONS.map((r) => <option key={r}>{r}</option>)}
                </select>
                <button className="primary" disabled={!reasonOk(reason)} onClick={() => run(() => api.post(`/api/monitoring/alerts/${id}/resolve`, { resolution, reason }), "Alert resolved.")}>
                  Resolve alert
                </button>
              </div>
            </div>
          )}
          {a.status === "RESOLVED" && <p className="muted">Resolved by {a.resolved_by} at {fmt(a.resolved_at)}: {a.resolution_reason}</p>}
        </Card>
      </div>
      <Card title={`Triage: ${a.triage_priority ?? "not scored"}${a.triage_score !== null && a.triage_score !== undefined ? ` · ${a.triage_score}/100` : ""}`}
        actions={<a href={`#/mule/${a.customer_id}`}>Money-mule indicators and flow →</a>}>
        {(a.triage_factors ?? []).length ? (
          <>
            <Bars data={(a.triage_factors as any[]).map((x) => ({ label: x.label, value: x.points, note: `/ ${x.max}` }))} max={25} />
            <details className="small" style={{ marginTop: 8 }}>
              <summary>Why each factor scored what it did</summary>
              <ul className="plain">{(a.triage_factors as any[]).map((x) => <li key={x.id}><b>{x.label}</b>: {x.points}/{x.max}. {x.reason}</li>)}</ul>
            </details>
            <p className="muted small">
              A heuristic ranking aid: fixed points for documented factors, summed to 100. It is not a probability of fraud, it never reads this alert's own
              outcome, and it only helps decide what to look at first. Computed {fmt(a.triage_computed_at)}.
            </p>
          </>
        ) : (
          <div className="muted">This alert was created before triage scoring existed. An administrator can re-score open alerts (POST /api/monitoring/triage/recompute).</div>
        )}
      </Card>
      <Card title={`Supporting transactions (${d.data.transactions.length})`}>
        <Table
          rows={d.data.transactions}
          empty="This detector's evidence does not reference individual transactions."
          columns={[
            { key: "transaction_id", label: "Transaction" },
            { key: "timestamp", label: "Time" },
            { key: "transaction_type", label: "Type" },
            { key: "channel", label: "Channel" },
            { key: "amount", label: "Amount", align: "right" },
            { key: "currency", label: "Cur" },
            { key: "amount_usd", label: "USD", align: "right" },
            { key: "country", label: "Country" },
            { key: "status", label: "Status" },
          ]}
        />
      </Card>
      <Card title="History (append-only)">
        <Table
          rows={d.data.events}
          columns={[
            { key: "ts", label: "When" },
            { key: "actor", label: "Who" },
            { key: "event_type", label: "Event" },
            { key: "from_status", label: "From" },
            { key: "to_status", label: "To" },
            { key: "detail", label: "Detail" },
          ]}
        />
      </Card>
    </div>
  );
}
