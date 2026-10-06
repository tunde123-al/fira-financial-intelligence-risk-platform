import { useState } from "react";
import { api, qs } from "../api";
import { Badge, Bars, Card, Field, ScoreBadge, Status, Table, errorText, fmt, useAsync } from "../components/ui";
import Copilot from "../components/Copilot";
import { entityHref, go } from "../router";

export default function CustomerPage({ id }: { id: string }) {
  const profile = useAsync(() => api.get(`/api/customers/${id}`), [id]);
  const [lookback, setLookback] = useState(90);
  const txns = useAsync(() => api.get(`/api/customers/${id}/transactions${qs({ lookback_days: lookback, limit: 200 })}`), [id, lookback]);
  const risk = useAsync(() => api.get(`/api/risk/customer/${id}`), [id]);
  const [invLookback, setInvLookback] = useState(30);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

  const investigate = async () => {
    setRunning(true);
    setRunError(null);
    try {
      const res = await api.post("/api/agent/investigate", {
        request: `Investigate customer ${id} and identify unusual activity during the last ${invLookback} days.`,
        subject_type: "customer",
        subject_id: id,
        lookback_days: invLookback,
      });
      if (res.investigation_id) go(`/investigations/${res.investigation_id}`);
      else setRunError(res.status_reason ?? res.status);
    } catch (e) {
      setRunError(errorText(e));
    } finally {
      setRunning(false);
    }
  };

  if (!profile.data) return <Status loading={profile.loading} error={profile.error} />;
  const c = profile.data.customer;
  return (
    <div className="page">
      <div className="page-head">
        <h2>
          Customer {c.customer_id} <Badge value={c.risk_profile} kind={c.risk_profile === "high" ? "high" : c.risk_profile === "medium" ? "medium" : "low"} />
        </h2>
        <div className="row">
          <Field label="Lookback (days)">
            <input type="number" min={1} max={365} value={invLookback} onChange={(e) => setInvLookback(Number(e.target.value))} style={{ width: 80 }} />
          </Field>
          <button className="primary" onClick={investigate} disabled={running}>
            {running ? "Investigating…" : "Run investigation"}
          </button>
          <a href={`#/graph?kind=customer&id=${c.customer_id}`}>Graph</a>
        </div>
      </div>
      {runError && <div className="error">{runError}</div>}
      <Copilot customerId={c.customer_id} />
      <div className="grid2">
        <Card title="Profile">
          <dl className="dl">
            <dt>Type / segment</dt>
            <dd>
              {c.customer_type} / {c.segment}
            </dd>
            <dt>Country / city</dt>
            <dd>
              {c.country} / {c.home_city}
            </dd>
            <dt>Customer since</dt>
            <dd>{fmt(c.created_at)}</dd>
            <dt>KYC level</dt>
            <dd>{c.kyc_level}</dd>
            <dt>Identifiers (masked)</dt>
            <dd>{profile.data.identifiers.map((i: any) => `${i.identifier_type}: ${i.masked_value}`).join(" · ")}</dd>
            <dt>Shares identifiers with</dt>
            <dd>{profile.data.shared_identifier_customers.length ? profile.data.shared_identifier_customers.map((s: any) => s.customer_id).join(", ") : "none"}</dd>
          </dl>
        </Card>
        <Card title="Current risk (deterministic engine, last 30 days)">
          {risk.data ? (
            <>
              <p>
                Score <ScoreBadge score={risk.data.score} band={risk.data.band} /> threshold {risk.data.investigation_threshold}
              </p>
              <Bars data={risk.data.contributors.map((x: any) => ({ label: x.signal_type, value: x.points, note: x.capped ? "(capped)" : "" }))} max={40} />
              {!risk.data.contributors.length && <p className="muted">No indicator crossed its threshold.</p>}
            </>
          ) : (
            <Status loading={risk.loading} error={risk.error} />
          )}
        </Card>
      </div>
      <Card title="Accounts">
        <Table
          rows={profile.data.accounts}
          columns={[
            { key: "account_id", label: "Account", render: (r) => <a href={entityHref("account", r.account_id)}>{r.account_id}</a> },
            { key: "account_type", label: "Type" },
            { key: "currency", label: "Currency" },
            { key: "status", label: "Status" },
            { key: "balance", label: "Balance", align: "right" },
            { key: "opened_at", label: "Opened" },
          ]}
        />
      </Card>
      <div className="grid2">
        <Card title="Alerts">
          <Table rows={profile.data.alerts} columns={[{ key: "alert_id", label: "Alert" }, { key: "alert_type", label: "Type" }, { key: "severity", label: "Severity", render: (r) => <Badge value={r.severity} kind={r.severity} /> }, { key: "status", label: "Status" }, { key: "created_at", label: "Created" }]} empty="No alerts" />
        </Card>
        <Card title="Previous investigations">
          <Table
            rows={profile.data.investigations}
            columns={[
              { key: "investigation_id", label: "Investigation", render: (r) => <a href={entityHref("investigation", r.investigation_id)}>{r.investigation_id}</a> },
              { key: "status", label: "Status" },
              { key: "conclusion", label: "Conclusion" },
              { key: "risk_score", label: "Risk", render: (r) => <ScoreBadge score={r.risk_score} /> },
            ]}
            empty="No previous investigations"
          />
        </Card>
      </div>
      <Card
        title={`Transactions (${txns.data?.total ?? "…"} in last ${lookback} days${txns.data?.truncated ? ", showing latest 200" : ""})`}
        actions={
          <select value={lookback} onChange={(e) => setLookback(Number(e.target.value))}>
            {[30, 90, 180, 365].map((d) => (
              <option key={d} value={d}>
                {d} days
              </option>
            ))}
          </select>
        }
      >
        {txns.data ? (
          <Table
            rows={txns.data.transactions}
            columns={[
              { key: "transaction_id", label: "ID" },
              { key: "timestamp", label: "Time" },
              { key: "transaction_type", label: "Type" },
              { key: "channel", label: "Channel" },
              { key: "amount", label: "Amount", align: "right", render: (r) => `${fmt(r.amount)} ${r.currency}` },
              { key: "amount_usd", label: "USD", align: "right" },
              { key: "direction", label: "Counterparty", render: (r) => r.merchant_id ?? r.receiver_account_id ?? r.external_counterparty ?? r.sender_account_id },
              { key: "country", label: "Country" },
              { key: "device_id", label: "Device" },
              { key: "status", label: "Status" },
            ]}
          />
        ) : (
          <Status loading={txns.loading} error={txns.error} />
        )}
      </Card>
    </div>
  );
}
