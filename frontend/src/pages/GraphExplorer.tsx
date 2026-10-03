import { FormEvent, useEffect, useState } from "react";
import { api, qs } from "../api";
import GraphView, { GNode } from "../components/GraphView";
import { Card, Field, JsonBlock, Status, errorText } from "../components/ui";
import { go } from "../router";

export default function GraphExplorer({ kind, id }: { kind?: string; id?: string }) {
  const [k, setK] = useState(kind ?? "customer");
  const [eid, setEid] = useState(id ?? "");
  const [depth, setDepth] = useState(2);
  const [graph, setGraph] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [src, setSrc] = useState("");
  const [dst, setDst] = useState("");
  const [path, setPath] = useState<any>(null);
  const [cluster, setCluster] = useState<any>(null);

  const load = async (kk: string, ii: string, d: number) => {
    if (!ii) return;
    setLoading(true);
    setError(null);
    setCluster(null);
    try {
      setGraph(await api.get(`/api/graph/${kk}/${ii}${qs({ depth: d })}`));
      if (kk === "customer") setCluster(await api.get(`/api/graph/cluster/${ii}`));
    } catch (e) {
      setError(errorText(e));
      setGraph(null);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    if (kind && id) {
      setK(kind);
      setEid(id);
      load(kind, id, depth);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, id]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    go(`/graph?kind=${k}&id=${eid.trim().toUpperCase()}`);
    load(k, eid.trim().toUpperCase(), depth);
  };

  const findPath = async (e: FormEvent) => {
    e.preventDefault();
    setPath(null);
    try {
      setPath(await api.get(`/api/graph/path${qs({ source: src.trim().toUpperCase(), target: dst.trim().toUpperCase() })}`));
    } catch (err) {
      setPath({ error: errorText(err) });
    }
  };

  const recenter = (n: GNode) => {
    const [kind2, id2] = n.id.split(":");
    if (["customer", "account", "device", "merchant"].includes(kind2)) {
      setK(kind2);
      setEid(id2);
    }
  };

  return (
    <div className="page">
      <h2>Graph Explorer</h2>
      <form className="row" onSubmit={submit}>
        <select value={k} onChange={(e) => setK(e.target.value)}>
          {["customer", "account", "device", "merchant"].map((x) => (
            <option key={x}>{x}</option>
          ))}
        </select>
        <input placeholder="Entity id, e.g. CUST-10291" value={eid} onChange={(e) => setEid(e.target.value)} />
        <Field label="Depth">
          <select value={depth} onChange={(e) => setDepth(Number(e.target.value))}>
            {[1, 2, 3].map((x) => (
              <option key={x}>{x}</option>
            ))}
          </select>
        </Field>
        <button className="primary">Explore</button>
      </form>
      {(loading || error) && <Status loading={loading} error={error} />}
      {graph && (
        <Card title={`Neighbourhood of ${graph.center} (${graph.nodes.length} nodes, ${graph.edges.length} edges${graph.truncated ? ", truncated" : ""})`}>
          <GraphView
            nodes={graph.nodes}
            edges={graph.edges}
            center={graph.center}
            highlight={path?.nodes ?? []}
            onSelect={recenter}
          />
          <p className="muted small">Click a node, then “Explore”, to re-centre on it.</p>
        </Card>
      )}
      {cluster && (
        <Card title="Cluster analytics">
          <p>
            {cluster.customers.length} customers · {cluster.accounts.length} accounts · {cluster.devices.length} devices · density {cluster.density}
          </p>
          <p>Flagged customers (open alerts): {cluster.flagged_customers.join(", ") || "none"}</p>
          <p>Shared devices: {cluster.shared_devices.map((s: any) => `${s.device_id} (${s.n_customers})`).join(", ") || "none"}</p>
          <h4>Most central entities</h4>
          <JsonBlock value={cluster.central_entities} />
        </Card>
      )}
      <Card title="Shortest transfer path between accounts">
        <form className="row" onSubmit={findPath}>
          <input placeholder="from ACC-…" value={src} onChange={(e) => setSrc(e.target.value)} />
          <input placeholder="to ACC-…" value={dst} onChange={(e) => setDst(e.target.value)} />
          <button>Find path</button>
        </form>
        {path && (path.error ? <div className="error">{path.error}</div> : path.found ? <p>{path.nodes.map((n: string) => n.split(":")[1]).join(" → ")} ({path.hops} hops)</p> : <p className="muted">No transfer path within the hop limit.</p>)}
      </Card>
    </div>
  );
}
