import { FormEvent, useEffect, useState } from "react";
import { api, qs } from "../api";
import { Card, JsonBlock, Table, errorText } from "../components/ui";
import { entityHref, go } from "../router";

export default function SearchPage({ initial }: { initial?: string }) {
  const [q, setQ] = useState(initial ?? "");
  const [type, setType] = useState("");
  const [hits, setHits] = useState<any[] | null>(null);
  const [detail, setDetail] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async (query: string) => {
    setError(null);
    setDetail(null);
    try {
      const res = await api.get<any[]>(`/api/search${qs({ q: query, entity_type: type || undefined })}`);
      setHits(res);
      if (res.length === 1 && res[0].entity_type === "transaction") open(res[0]);
    } catch (e) {
      setError(errorText(e));
    }
  };
  useEffect(() => {
    if (initial) run(initial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initial]);

  const open = async (h: any) => {
    if (h.entity_type === "customer") return go(`/customers/${h.entity_id}`);
    try {
      const paths: Record<string, string> = { account: "accounts", transaction: "transactions", merchant: "merchants", device: "devices" };
      const path = paths[String(h.entity_type)];
      setDetail({ type: h.entity_type, id: h.entity_id, data: await api.get(`/api/${path}/${h.entity_id}`) });
    } catch (e) {
      setError(errorText(e));
    }
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (q.trim().length >= 2) run(q.trim());
  };

  const owner = detail?.data?.owner?.customer_id ?? detail?.data?.account?.customer_id;
  return (
    <div className="page">
      <h2>Investigation Search</h2>
      <form className="row" onSubmit={submit}>
        <input placeholder="CUST-10291, ACC-200001, TXN-00001234, DEV-300010, MER-5000 or merchant name" value={q} onChange={(e) => setQ(e.target.value)} style={{ flex: 1 }} />
        <select value={type} onChange={(e) => setType(e.target.value)}>
          <option value="">all types</option>
          {["customer", "account", "transaction", "merchant", "device"].map((t) => (
            <option key={t}>{t}</option>
          ))}
        </select>
        <button className="primary">Search</button>
      </form>
      {error && <div className="error">{error}</div>}
      {hits && (
        <Card title={`${hits.length} result(s)`}>
          <Table
            rows={hits}
            columns={[
              { key: "entity_type", label: "Type" },
              { key: "entity_id", label: "ID", render: (r) => <button className="link" onClick={() => open(r)}>{r.entity_id}</button> },
              { key: "label", label: "Description" },
            ]}
          />
        </Card>
      )}
      {detail && (
        <Card
          title={`${detail.type} ${detail.id}`}
          actions={
            <>
              {owner && <a href={entityHref("customer", owner)}>Open owner {owner}</a>}{" "}
              {detail.type !== "transaction" && <a href={entityHref(detail.type, detail.id)}>Graph</a>}
            </>
          }
        >
          <JsonBlock value={detail.data} />
        </Card>
      )}
    </div>
  );
}
