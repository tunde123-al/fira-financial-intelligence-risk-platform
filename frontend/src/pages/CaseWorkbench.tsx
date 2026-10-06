import { useState } from "react";
import { Session, api } from "../api";
import GraphView from "../components/GraphView";
import { Badge, Bars, Card, JsonBlock, ScoreBadge, Status, Table, Tabs, errorText, fmt, useAsync } from "../components/ui";
import { EVIDENCE_LABELS, caseActions, reasonOk } from "../lib/workflow";

const TABS = ["Overview", "Rules", "Transactions", "Activity", "Counterparties", "Network", "Alerts", "Documents", "Evidence", "Notes", "Timeline", "Decision"];

function EvidenceBadge({ cls }: { cls: string }) {
  return <span className={`kind kind-${cls === "LLM_GENERATED_SUMMARY" ? "ml" : "x"}`}>{EVIDENCE_LABELS[cls] ?? cls}</span>;
}

export default function CaseWorkbenchPage({ id, session }: { id: string; session: Session }) {
  const wb = useAsync(() => api.get<any>(`/api/cases/${id}/workbench`), [id]);
  const [tab, setTab] = useState("Overview");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [decision, setDecision] = useState("CLEARED");
  const [reason, setReason] = useState("");
  const [evKind, setEvKind] = useState("transaction");
  const [evRef, setEvRef] = useState("");
  if (!wb.data) return <Status loading={wb.loading} error={wb.error} />;
  const w = wb.data;
  const c = w.case;
  const acts = caseActions(c.status);
  const mine = c.assigned_to === `U-${session.username}` || session.role === "admin";

  const act = async (fn: () => Promise<unknown>, ok?: string) => {
    setBusy(true);
    setMsg(null);
    try {
      await fn();
      if (ok) setMsg(ok);
      wb.reload();
    } catch (e) {
      setMsg(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h2>
            {c.case_number} <Badge value={c.status} /> <Badge value={c.priority} kind={c.priority} />
            {c.decision && <Badge value={c.decision} />}
          </h2>
          <p className="muted">
            {c.title} · customer <a href={`#/customers/${c.customer_id}`}>{c.customer_id}</a> · assigned to {c.assigned_to ?? "nobody"} · opened {fmt(c.opened_at)}
          </p>
        </div>
        <div className="row">
          <ScoreBadge score={w.risk.score} band={w.risk.band} />
          {!c.assigned_to && c.status !== "CLOSED" && (
            <button onClick={() => act(() => api.post(`/api/cases/${id}/assign`, { assignee: `U-${session.username}` }))}>Assign to me</button>
          )}
          {c.status !== "CLOSED" && mine && (
            <button disabled={busy} onClick={() => act(() => api.post(`/api/cases/${id}/investigate`, {}), "Investigation run and linked to this case.")}>
              {busy ? "Running…" : "Run evidence-grounded investigation"}
            </button>
          )}
        </div>
      </div>
      {msg && <div className="notice">{msg}</div>}
      <Tabs tabs={TABS} current={tab} onChange={setTab} />

      {tab === "Overview" && (
        <div className="grid2">
          <Card title="Customer profile">
            <dl className="dl">
              <dt>Customer</dt><dd>{w.customer.customer.customer_id}</dd>
              <dt>Type / segment</dt><dd>{w.customer.customer.customer_type} / {w.customer.customer.segment}</dd>
              <dt>Country / city</dt><dd>{w.customer.customer.country} / {fmt(w.customer.customer.home_city)}</dd>
              <dt>Risk profile</dt><dd>{w.customer.customer.risk_profile}</dd>
              <dt>Status / KYC level</dt><dd>{w.customer.customer.status} / {fmt(w.customer.customer.kyc_level)}</dd>
              <dt>Accounts</dt><dd>{w.customer.accounts.map((a: any) => a.account_id).join(", ")}</dd>
              <dt>Identifiers</dt><dd>{w.customer.identifiers.map((i: any) => `${i.identifier_type} ${i.masked_value}`).join("; ")}</dd>
            </dl>
          </Card>
          <Card title="Risk score (deterministic)">
            <p>{w.risk.explanation}</p>
            <Bars data={w.risk.score_components.map((x: any) => ({ label: x.label, value: Math.max(0, x.points) }))} max={100} />
            <p className="muted small">Not part of the score: {w.risk.not_in_score.join(", ")}.</p>
          </Card>
        </div>
      )}

      {tab === "Rules" && (
        <Card title="Triggered rules for this case">
          <Table
            rows={w.triggered_rules}
            columns={[
              { key: "alert_id", label: "Alert", render: (r) => <a href={`#/alerts/${r.alert_id}`}>{r.alert_id}</a> },
              { key: "detector_id", label: "Detector" },
              { key: "severity", label: "Severity", render: (r) => <Badge value={r.severity} kind={r.severity} /> },
              { key: "risk_contribution", label: "Points", align: "right" },
              { key: "status", label: "Status", render: (r) => <Badge value={r.status} /> },
              { key: "description", label: "Explanation" },
            ]}
          />
          <h4>All detectors for this customer (current window)</h4>
          <Table
            rows={w.risk.detector_results}
            columns={[
              { key: "name", label: "Detector" },
              { key: "triggered", label: "Triggered", render: (r) => (r.triggered ? <Badge value="yes" kind="high" /> : <span className="muted">no</span>) },
              { key: "observed", label: "Observed" },
              { key: "threshold", label: "Threshold" },
              { key: "risk_contribution", label: "Points", align: "right" },
              { key: "reason", label: "Reason" },
            ]}
          />
        </Card>
      )}

      {tab === "Transactions" && (
        <>
          <Card title={`Transactions that caused the alerts (${w.related_transactions.length})`}>
            <TxTable rows={w.related_transactions} />
          </Card>
          <Card title="Most recent transactions (last 50 in the monitoring window)">
            <TxTable rows={w.recent_transactions} />
          </Card>
        </>
      )}

      {tab === "Activity" && (
        <Card title="Account activity by time window versus baseline">
          <Table
            rows={w.account_activity.windows}
            columns={[
              { key: "window", label: "Window" },
              { key: "out", label: "Outbound (count / USD)", render: (r) => `${r.outbound.count} / ${r.outbound.volume_usd.toLocaleString()}` },
              { key: "in", label: "Inbound (count / USD)", render: (r) => `${r.inbound.count} / ${r.inbound.volume_usd.toLocaleString()}` },
              { key: "distinct_beneficiaries", label: "Beneficiaries" },
              { key: "distinct_senders", label: "Senders" },
              { key: "exp", label: "Baseline expected outbound", render: (r) => String(r.baseline_expected.outbound_count) },
              { key: "ratio", label: "× baseline", render: (r) => fmt(r.outbound_count_vs_baseline) },
            ]}
          />
          <p className="muted small">{w.account_activity.note}</p>
        </Card>
      )}

      {tab === "Counterparties" && (
        <>
          <Card title="Direct counterparties (degree 1)">
            <CpTable rows={w.counterparties.direct} />
          </Card>
          <Card title="Second-degree counterparties">
            <CpTable rows={w.counterparties.second_degree} />
          </Card>
          <Card title="Beneficiaries also paid by other customers">
            <Table
              rows={w.counterparties.shared_beneficiaries}
              empty="None found in the window."
              columns={[
                { key: "beneficiary_account", label: "Beneficiary account" },
                { key: "beneficiary_owner", label: "Owner" },
                { key: "paid_from", label: "Paid from" },
                { key: "n_other_customers", label: "Other customers" },
                { key: "c", label: "Customers", render: (r) => r.customers.map((x: any) => x.customer_id).join(", ") },
              ]}
            />
            <p className="muted small">{w.counterparties.note}</p>
          </Card>
        </>
      )}

      {tab === "Network" && (
        <Card title={`Transaction network (${w.network.graph.nodes.length} nodes, ${w.network.graph.edges.length} edges)`}>
          <GraphView nodes={w.network.graph.nodes} edges={w.network.graph.edges} center={w.network.graph.center} highlight={[]} onSelect={() => undefined} />
          <p className="muted small">
            Cluster: {w.network.cluster.customers.length} customers, {w.network.cluster.flagged_customers.length} with legacy open alerts, density {w.network.cluster.density}.
            <a href={`#/graph?kind=customer&id=${c.customer_id}`}> Open in Graph Explorer</a>
          </p>
        </Card>
      )}

      {tab === "Alerts" && (
        <>
          <Card title="Other FIRA monitoring alerts for this customer">
            <Table rows={w.related_alerts.monitoring} empty="None" columns={[
              { key: "alert_id", label: "Alert", render: (r) => <a href={`#/alerts/${r.alert_id}`}>{r.alert_id}</a> },
              { key: "detector_id", label: "Detector" }, { key: "status", label: "Status" }, { key: "resolution", label: "Resolution" },
              { key: "triggered_at", label: "Triggered", render: (r) => fmt(r.triggered_at) },
            ]} />
          </Card>
          <Card title="Legacy seeded alerts (synthetic)">
            <Table rows={w.related_alerts.legacy_seeded} empty="None" columns={[
              { key: "alert_id", label: "Alert" }, { key: "alert_type", label: "Type" }, { key: "severity", label: "Severity" },
              { key: "status", label: "Status" }, { key: "created_at", label: "Created", render: (r) => fmt(r.created_at) },
            ]} />
          </Card>
        </>
      )}

      {tab === "Documents" && (
        <Card title="Policy and procedure passages retrieved for the triggered detectors">
          {w.documents.length === 0 && <div className="muted">No passages (no document index, or no playbook query for these detectors).</div>}
          {w.documents.map((d: any) => (
            <div key={d.chunk_id} className="passage evidence">
              <h4>{d.title} <EvidenceBadge cls="DOCUMENT_EVIDENCE" /></h4>
              <p className="muted small">{d.section} · document {d.document_id} · chunk {d.chunk_id} · retrieved for {d.retrieved_for}</p>
              <blockquote>{d.excerpt}</blockquote>
              {c.status !== "CLOSED" && mine && (
                <button className="link" onClick={() => act(() => api.post(`/api/cases/${id}/evidence`, { kind: "document", ref: d.chunk_id }), "Passage attached.")}>Attach to case</button>
              )}
            </div>
          ))}
        </Card>
      )}

      {tab === "Evidence" && (
        <>
          <Card title="What each kind of evidence means">
            <div className="row wrap">
              {Object.entries(w.evidence.classes).map(([k, v]) => <span key={k} className="chip">{String(v)}</span>)}
            </div>
            <p className="muted small">AI-generated text is never a source of fact: it is drafted from the stored evidence below and labelled.</p>
          </Card>
          <Card title="Rule results (from the alerts)">
            {w.evidence.rule_results.map((r: any) => (
              <div key={r.alert_id} className="evidence">
                <EvidenceBadge cls={r.evidence_class} /> <strong>{r.detector_id}</strong> <a href={`#/alerts/${r.alert_id}`}>{r.alert_id}</a>
                <div>{r.explanation?.summary}</div>
              </div>
            ))}
          </Card>
          <Card title="Attached by investigators">
            {w.evidence.attached.length === 0 && <div className="muted">Nothing attached yet.</div>}
            {w.evidence.attached.map((e: any) => (
              <div key={e.evidence_id} className="evidence">
                <EvidenceBadge cls={e.evidence_class} /> <strong>{e.title}</strong> <span className="muted small">by {e.added_by} · {fmt(e.created_at)} · {e.source}</span>
                <JsonBlock value={e.content} />
              </div>
            ))}
            {c.status !== "CLOSED" && mine && (
              <div className="row wrap" style={{ marginTop: 8 }}>
                <select value={evKind} onChange={(e) => setEvKind(e.target.value)} aria-label="evidence kind">
                  <option value="transaction">transaction</option>
                  <option value="document">document passage (chunk id)</option>
                  <option value="alert">alert result</option>
                </select>
                <input placeholder="TXN-… / chunk id / MAL-…" value={evRef} onChange={(e) => setEvRef(e.target.value)} style={{ width: 280 }} />
                <button disabled={evRef.trim().length < 3} onClick={() => act(() => api.post(`/api/cases/${id}/evidence`, { kind: evKind, ref: evRef.trim() }), "Evidence attached.")}>Attach</button>
                <span className="muted small">Only existing objects are accepted.</span>
              </div>
            )}
          </Card>
          <Card title="Evidence from the evidence-grounded investigation">
            {!w.investigation && <div className="muted">No investigation linked. Use "Run evidence-grounded investigation".</div>}
            {w.investigation && (
              <>
                <p className="muted small">
                  Investigation <a href={`#/investigations/${w.investigation.investigation_id}`}>{w.investigation.investigation_id}</a> · status {w.investigation.status}
                </p>
                {w.evidence.investigation.map((e: any) => (
                  <div key={e.evidence_id} className="evidence">
                    <strong>{e.ref}</strong> <EvidenceBadge cls={e.evidence_class} /> {e.title}
                  </div>
                ))}
                <h4>Narrative</h4>
                <ul className="claims">
                  {w.investigation.narrative.map((n: any, i: number) => (
                    <li key={i}>
                      {n.text} {(n.citations ?? []).map((r: string) => <span key={r} className="cite">{r}</span>)}
                      {n.ai_generated ? <EvidenceBadge cls="LLM_GENERATED_SUMMARY" /> : <span className="kind">rule-generated text</span>}
                    </li>
                  ))}
                </ul>
                <p className="muted small">{w.investigation.narrative_note}</p>
              </>
            )}
          </Card>
        </>
      )}

      {tab === "Notes" && (
        <Card title="Investigation notes (append-only)">
          {w.notes.length === 0 && <div className="muted">No notes yet.</div>}
          {w.notes.map((n: any) => (
            <div key={n.note_id} className="evidence">
              <span className="muted small">{n.author} · {fmt(n.created_at)}</span>
              <div style={{ whiteSpace: "pre-wrap" }}>{n.body}</div>
            </div>
          ))}
          {c.status !== "CLOSED" && mine && (
            <div className="decision" style={{ marginTop: 10 }}>
              <textarea rows={3} value={note} placeholder="Add a note (it cannot be edited or deleted afterwards)" onChange={(e) => setNote(e.target.value)} />
              <div><button className="primary" disabled={!note.trim()} onClick={() => act(async () => { await api.post(`/api/cases/${id}/notes`, { body: note }); setNote(""); })}>Add note</button></div>
            </div>
          )}
        </Card>
      )}

      {tab === "Timeline" && (
        <Card title="Timeline">
          <ul className="timeline">
            {w.timeline.map((t: any, i: number) => (
              <li key={i}>
                <span className="muted small">{fmt(t.ts)}</span> <strong>{t.source}</strong> {t.event}
                {t.from || t.to ? <> ({t.from ?? "—"} → {t.to ?? "—"})</> : null} <span className="muted small">by {t.actor}</span>
                {t.detail?.body ? <div className="muted small">{String(t.detail.body).slice(0, 200)}</div> : null}
                {t.detail?.reason ? <div className="muted small">{String(t.detail.reason)}</div> : null}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {tab === "Decision" && (
        <Card title="Investigator decision">
          {c.status === "CLOSED" ? (
            <p>
              Closed with <strong>{c.decision}</strong> by {c.decided_by} at {fmt(c.closed_at)}: {c.decision_reason}
            </p>
          ) : (
            <div className="decision">
              {c.decision && <p className="muted">Latest decision: {c.decision} by {c.decided_by}: {c.decision_reason}</p>}
              <div className="row wrap">
                {acts.transitions.map((t) => (
                  <button key={t} disabled={!mine} onClick={() => act(() => api.post(`/api/cases/${id}/transition`, { status: t }))}>Move to {t}</button>
                ))}
              </div>
              {acts.decisions.length === 0 ? (
                <p className="muted">Start the investigation (move the case to INVESTIGATING) before recording a decision.</p>
              ) : (
                <>
                  <div className="row wrap">
                    {acts.decisions.map((d) => (
                      <label key={d} className="radio"><input type="radio" name="d" checked={decision === d} onChange={() => setDecision(d)} /> {d}</label>
                    ))}
                  </div>
                  <label className="field">
                    <span>Reason (required, at least 5 characters). Recorded in the audit trail with your user id.</span>
                    <textarea rows={3} value={reason} onChange={(e) => setReason(e.target.value)} />
                  </label>
                  <div>
                    <button className="primary" disabled={!mine || !reasonOk(reason) || !acts.decisions.includes(decision as any)} onClick={() => act(() => api.post(`/api/cases/${id}/decision`, { decision, reason }), "Decision recorded.")}>
                      Record decision as {session.username}
                    </button>
                  </div>
                  <p className="muted small">CLEARED, FALSE_POSITIVE and CONFIRMED_SUSPICIOUS close the case and resolve its alerts. ESCALATED keeps it open for senior review. FIRA never freezes accounts or files reports.</p>
                </>
              )}
            </div>
          )}
        </Card>
      )}
    </div>
  );
}

function TxTable({ rows }: { rows: any[] }) {
  return (
    <Table
      rows={rows}
      empty="No transactions"
      columns={[
        { key: "transaction_id", label: "Transaction" },
        { key: "timestamp", label: "Time", render: (r) => fmt(r.timestamp) },
        { key: "transaction_type", label: "Type" },
        { key: "channel", label: "Channel" },
        { key: "sender_account_id", label: "From" },
        { key: "receiver_account_id", label: "To", render: (r) => r.receiver_account_id ?? r.external_counterparty ?? r.merchant_id ?? "—" },
        { key: "amount_usd", label: "USD", align: "right" },
        { key: "country", label: "Country" },
        { key: "status", label: "Status" },
      ]}
    />
  );
}

function CpTable({ rows }: { rows: any[] }) {
  return (
    <Table
      rows={rows}
      empty="None in the window"
      columns={[
        { key: "account_id", label: "Account", render: (r) => <a href={`#/graph?kind=account&id=${r.account_id}`}>{r.account_id}</a> },
        { key: "owner_customer_id", label: "Owner", render: (r) => (r.owner_customer_id ? <a href={`#/customers/${r.owner_customer_id}`}>{r.owner_customer_id}</a> : "—") },
        { key: "direction", label: "Direction" },
        { key: "n_transactions", label: "Txns", align: "right" },
        { key: "total_usd", label: "USD", align: "right" },
        { key: "via_account", label: "Via" },
      ]}
    />
  );
}
