import { useState } from "react";
import { api, qs } from "../api";
import { Badge, Card, Status, Table, errorText, fmt, useAsync } from "../components/ui";

const STATUSES = ["OPEN", "INVESTIGATING", "ESCALATED", "PENDING_REVIEW", "CLOSED"];

export default function CasesPage() {
  const [status, setStatus] = useState("");
  const [assigned, setAssigned] = useState("");
  const [q, setQ] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [customer, setCustomer] = useState("");
  const list = useAsync(() => api.get<any>(`/api/cases${qs({ status: status || undefined, assigned_to: assigned || undefined, q: q || undefined, limit: 100 })}`), [status, assigned, q]);

  const open = async () => {
    setMsg(null);
    try {
      const r: any = await api.post("/api/cases", { customer_id: customer.trim().toUpperCase(), alert_ids: [] });
      window.location.hash = `#/cases/${r.case.case_id}`;
    } catch (e) {
      setMsg(errorText(e));
    }
  };

  return (
    <div className="page">
      <h2>Cases</h2>
      <Card
        title={`${list.data?.total ?? 0} case(s)`}
        actions={
          <div className="row wrap">
            <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="status">
              <option value="">all statuses</option>
              {STATUSES.map((s) => <option key={s}>{s}</option>)}
            </select>
            <select value={assigned} onChange={(e) => setAssigned(e.target.value)} aria-label="assignee">
              <option value="">any investigator</option>
              <option value="me">assigned to me</option>
              <option value="unassigned">unassigned</option>
            </select>
            <input placeholder="search case, customer" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
        }
      >
        {list.data ? (
          <Table
            rows={list.data.items}
            empty="No cases yet. Create one from an alert."
            columns={[
              { key: "case_number", label: "Case", render: (r) => <a href={`#/cases/${r.case_id}`}>{r.case_number}</a> },
              { key: "status", label: "Status", render: (r) => <Badge value={r.status} /> },
              { key: "priority", label: "Priority", render: (r) => <Badge value={r.priority} kind={r.priority} /> },
              { key: "customer_id", label: "Customer", render: (r) => <a href={`#/customers/${r.customer_id}`}>{r.customer_id}</a> },
              { key: "title", label: "Title" },
              { key: "assigned_to", label: "Assigned" },
              { key: "decision", label: "Decision" },
              { key: "opened_at", label: "Opened", render: (r) => fmt(r.opened_at) },
              { key: "updated_at", label: "Updated", render: (r) => fmt(r.updated_at) },
            ]}
          />
        ) : (
          <Status loading={list.loading} error={list.error} />
        )}
      </Card>
      <Card title="Open a case manually">
        <div className="row">
          <input placeholder="CUST-…" value={customer} onChange={(e) => setCustomer(e.target.value)} />
          <button disabled={!/^CUST-\d+$/i.test(customer.trim())} onClick={open}>Create case</button>
        </div>
        {msg && <div className="error" style={{ marginTop: 8 }}>{msg}</div>}
      </Card>
    </div>
  );
}
