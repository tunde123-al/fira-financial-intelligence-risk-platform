import { useState } from "react";
import { api, qs } from "../api";
import { Badge, Card, ScoreBadge, Status, Table, useAsync } from "../components/ui";
import { entityHref } from "../router";

export default function InvestigationsPage() {
  const [status, setStatus] = useState("");
  const list = useAsync(() => api.get<any[]>(`/api/investigations${qs({ status: status || undefined, limit: 200 })}`), [status]);
  return (
    <div className="page">
      <h2>Investigations</h2>
      <Card
        title="Investigation queue"
        actions={
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">all statuses</option>
            {["pending_review", "in_progress", "open", "needs_input", "closed", "failed"].map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        }
      >
        {list.data ? (
          <Table
            rows={list.data}
            columns={[
              { key: "investigation_id", label: "Investigation", render: (r) => <a href={entityHref("investigation", r.investigation_id)}>{r.investigation_id}</a> },
              { key: "subject_id", label: "Subject", render: (r) => <a href={entityHref(r.subject_type, r.subject_id)}>{r.subject_id}</a> },
              { key: "status", label: "Status", render: (r) => <Badge value={r.status} /> },
              { key: "risk_score", label: "Risk", render: (r) => <ScoreBadge score={r.risk_score} /> },
              { key: "signals", label: "Signals" },
              { key: "conclusion", label: "Conclusion" },
              { key: "assigned_to", label: "Assigned" },
              { key: "created_at", label: "Created" },
            ]}
          />
        ) : (
          <Status loading={list.loading} error={list.error} />
        )}
      </Card>
    </div>
  );
}
