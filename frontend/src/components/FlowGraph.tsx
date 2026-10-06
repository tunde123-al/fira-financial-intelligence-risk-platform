import { useMemo, useState } from "react";
import { FlowEdge, FlowNode, layoutFlow, strokeFor, usd } from "../lib/flow";

// Directed money-flow diagram. Nodes are accounts, arrows go from the sending to the receiving account, the arrow label
// is the total USD and the number of transfers. Flagged accounts (owner has an open alert) are outlined in red.
export default function FlowGraph({ nodes, edges }: { nodes: FlowNode[]; edges: FlowEdge[] }) {
  const { placed, width, height } = useMemo(() => layoutFlow(nodes), [nodes]);
  const [hover, setHover] = useState<string | null>(null);
  const pos = useMemo(() => Object.fromEntries(placed.map((p) => [p.account_id, p])), [placed]);
  if (!nodes.length) return <div className="muted">No account-to-account transfers in this window.</div>;
  const NODE_W = 128;
  const NODE_H = 34;
  return (
    <div className="flow-wrap">
      <div className="legend">
        <span className="legend-item"><span className="dot" style={{ background: "var(--accent)" }} /> customer's account</span>
        <span className="legend-item"><span className="dot" style={{ background: "#64748b" }} /> counterparty account</span>
        <span className="legend-item"><span className="dot" style={{ background: "var(--bad)" }} /> owner has an open alert</span>
        <span className="muted">arrows: sender → receiver; label: total USD × number of transfers</span>
      </div>
      <svg role="img" aria-label="Money flow between accounts" viewBox={`0 0 ${width} ${height}`} style={{ width: "100%", maxHeight: 560 }}>
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8" />
          </marker>
        </defs>
        {edges.map((e, i) => {
          const s = pos[e.source];
          const t = pos[e.target];
          if (!s || !t) return null;
          const sx = s.x + NODE_W;
          const tx = t.x;
          const forward = t.x >= s.x;
          const x1 = forward ? sx : s.x;
          const x2 = forward ? tx : t.x + NODE_W;
          const y1 = s.y + NODE_H / 2;
          const y2 = t.y + NODE_H / 2;
          const mx = (x1 + x2) / 2;
          const active = hover === e.source || hover === e.target;
          return (
            <g key={i} opacity={hover && !active ? 0.25 : 1}>
              <path d={`M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`} fill="none" stroke="#94a3b8"
                strokeWidth={strokeFor(e.total_usd)} markerEnd="url(#arrow)" />
              <text x={mx} y={(y1 + y2) / 2 - 4} textAnchor="middle" className="node-label">{usd(e.total_usd)} × {e.n}</text>
              <title>{`${e.source} → ${e.target}: USD ${e.total_usd.toLocaleString()} in ${e.n} transfer(s), ${e.first_ts ?? "?"} to ${e.last_ts ?? "?"}`}</title>
            </g>
          );
        })}
        {placed.map((n) => (
          <g key={n.account_id} transform={`translate(${n.x},${n.y})`} className="node" onMouseEnter={() => setHover(n.account_id)}
            onMouseLeave={() => setHover(null)} onClick={() => { window.location.hash = `#/graph?kind=account&id=${n.account_id}`; }}>
            <rect width={NODE_W} height={NODE_H} rx={6} fill={n.depth === 0 ? "var(--accent)" : "#1e293b"}
              stroke={n.flagged ? "var(--bad)" : "#475569"} strokeWidth={n.flagged ? 3 : 1} />
            <text x={8} y={14} fill="#f8fafc" fontSize={11}>{n.account_id}</text>
            <text x={8} y={27} fill="#cbd5e1" fontSize={9}>{n.owner_customer_id ?? "no owner"} · in {n.in_degree} / out {n.out_degree}</text>
            <title>{`${n.account_id} (${n.roles.join(", ")}), depth ${n.depth}${n.flagged ? ", owner has an open alert" : ""}${n.is_hub ? ", hub (not expanded)" : ""}`}</title>
          </g>
        ))}
      </svg>
    </div>
  );
}
