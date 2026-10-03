import { useState } from "react";
import { Session, api, qs } from "../api";
import { Badge, Card, Status, Table, useAsync } from "../components/ui";

export default function AuditPage({ session }: { session: Session }) {
  const [action, setAction] = useState("");
  const [user, setUser] = useState("");
  const log = useAsync(() => api.get<any[]>(`/api/audit${qs({ limit: 500, action: action || undefined, user_id: user || undefined })}`), [action, user]);
  return (
    <div className="page">
      <h2>Audit Log</h2>
      {session.role !== "admin" && <p className="muted">Analysts see their own activity; administrators see all users.</p>}
      <div className="row">
        <select value={action} onChange={(e) => setAction(e.target.value)}>
          <option value="">all actions</option>
          {["tool_call", "agent_investigate", "human_decision", "login", "create_investigation", "config_proposal", "config_approve", "evaluation_run", "documents_ingest", "document_upload", "unmask"].map((a) => (
            <option key={a}>{a}</option>
          ))}
        </select>
        {session.role === "admin" && <input placeholder="user id" value={user} onChange={(e) => setUser(e.target.value)} />}
      </div>
      <Card title="Events (most recent first)">
        {log.data ? (
          <Table
            rows={log.data}
            columns={[
              { key: "ts", label: "Time" },
              { key: "user_id", label: "User" },
              { key: "role", label: "Role" },
              { key: "action", label: "Action" },
              { key: "tool", label: "Tool" },
              { key: "entity", label: "Entity", render: (r) => (r.entity_id ? `${r.entity_type}:${r.entity_id}` : "") },
              { key: "result", label: "Result", render: (r) => <Badge value={r.result} kind={r.result === "ok" ? "low" : "high"} /> },
              { key: "request_id", label: "Request" },
              { key: "details", label: "Details", render: (r) => <code>{JSON.stringify(r.details)}</code> },
            ]}
          />
        ) : (
          <Status loading={log.loading} error={log.error} />
        )}
      </Card>
    </div>
  );
}
