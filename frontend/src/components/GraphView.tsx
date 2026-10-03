import { useMemo, useState } from "react";

// Interactive SVG graph with a deterministic force-directed layout (no external libraries).

export interface GNode {
  id: string;
  kind: string;
  label: string;
  props?: Record<string, any>;
}
export interface GEdge {
  source: string;
  target: string;
  rel: string;
  props?: Record<string, any>;
}

const COLORS: Record<string, string> = {
  customer: "#2563eb",
  account: "#0d9488",
  device: "#d97706",
  merchant: "#7c3aed",
  ip: "#64748b",
  phone: "#db2777",
  email: "#db2777",
  address: "#db2777",
};

function layout(nodes: GNode[], edges: GEdge[], w: number, h: number, center?: string) {
  const n = nodes.length;
  const idx = new Map(nodes.map((nd, i) => [nd.id, i]));
  // deterministic initial positions on a spiral
  const pos = nodes.map((_, i) => {
    const a = i * 2.399963;
    const r = 18 * Math.sqrt(i + 1);
    return { x: w / 2 + r * Math.cos(a), y: h / 2 + r * Math.sin(a) };
  });
  const k = Math.sqrt((w * h) / Math.max(n, 1)) * 0.6;
  let t = w / 8;
  const links = edges.map((e) => [idx.get(e.source), idx.get(e.target)]).filter((p): p is [number, number] => p[0] !== undefined && p[1] !== undefined);
  for (let it = 0; it < 250; it++) {
    const disp = nodes.map(() => ({ x: 0, y: 0 }));
    for (let i = 0; i < n; i++)
      for (let j = i + 1; j < n; j++) {
        const dx = pos[i].x - pos[j].x;
        const dy = pos[i].y - pos[j].y;
        const d = Math.max(Math.hypot(dx, dy), 0.01);
        const f = (k * k) / d;
        disp[i].x += (dx / d) * f;
        disp[i].y += (dy / d) * f;
        disp[j].x -= (dx / d) * f;
        disp[j].y -= (dy / d) * f;
      }
    for (const [a, b] of links) {
      const dx = pos[a].x - pos[b].x;
      const dy = pos[a].y - pos[b].y;
      const d = Math.max(Math.hypot(dx, dy), 0.01);
      const f = (d * d) / k;
      disp[a].x -= (dx / d) * f;
      disp[a].y -= (dy / d) * f;
      disp[b].x += (dx / d) * f;
      disp[b].y += (dy / d) * f;
    }
    for (let i = 0; i < n; i++) {
      const d = Math.max(Math.hypot(disp[i].x, disp[i].y), 0.01);
      pos[i].x = Math.min(w - 20, Math.max(20, pos[i].x + (disp[i].x / d) * Math.min(d, t)));
      pos[i].y = Math.min(h - 20, Math.max(20, pos[i].y + (disp[i].y / d) * Math.min(d, t)));
    }
    t *= 0.98;
  }
  if (center && idx.has(center)) {
    const c = pos[idx.get(center)!];
    const dx = w / 2 - c.x;
    const dy = h / 2 - c.y;
    pos.forEach((p) => {
      p.x = Math.min(w - 20, Math.max(20, p.x + dx));
      p.y = Math.min(h - 20, Math.max(20, p.y + dy));
    });
  }
  return { pos, idx };
}

export default function GraphView({
  nodes,
  edges,
  center,
  highlight = [],
  onSelect,
  height = 560,
}: {
  nodes: GNode[];
  edges: GEdge[];
  center?: string;
  highlight?: string[];
  onSelect?: (n: GNode) => void;
  height?: number;
}) {
  const W = 960;
  const H = height;
  const [selected, setSelected] = useState<GNode | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const visible = useMemo(() => nodes.filter((n) => !hidden.has(n.kind)), [nodes, hidden]);
  const visibleIds = useMemo(() => new Set(visible.map((n) => n.id)), [visible]);
  const vEdges = useMemo(() => edges.filter((e) => visibleIds.has(e.source) && visibleIds.has(e.target)), [edges, visibleIds]);
  const { pos, idx } = useMemo(() => layout(visible, vEdges, W, H, center), [visible, vEdges, H, center]);
  const hl = new Set(highlight);
  const kinds = Array.from(new Set(nodes.map((n) => n.kind)));
  if (!nodes.length) return <div className="muted">No graph data for this entity.</div>;
  return (
    <div className="graph">
      <div className="legend">
        {kinds.map((k) => (
          <label key={k} className="legend-item">
            <input
              type="checkbox"
              checked={!hidden.has(k)}
              onChange={() => {
                const s = new Set(hidden);
                if (s.has(k)) s.delete(k);
                else s.add(k);
                setHidden(s);
              }}
            />
            <span className="dot" style={{ background: COLORS[k] ?? "#94a3b8" }} /> {k} ({nodes.filter((n) => n.kind === k).length})
          </label>
        ))}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Entity relationship graph">
        {vEdges.map((e, i) => {
          const a = pos[idx.get(e.source)!];
          const b = pos[idx.get(e.target)!];
          const on = hl.has(e.source) && hl.has(e.target);
          return (
            <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y} className={`edge rel-${e.rel} ${on ? "edge-hl" : ""}`}>
              <title>
                {e.rel} {e.props?.total_usd ? `· USD ${Number(e.props.total_usd).toLocaleString()}` : ""} {e.props?.n ? `· ${e.props.n} txns` : ""}
              </title>
            </line>
          );
        })}
        {visible.map((n, i) => {
          const p = pos[i];
          const isCenter = n.id === center;
          const flagged = n.props?.open_alert;
          return (
            <g
              key={n.id}
              transform={`translate(${p.x},${p.y})`}
              className="node"
              onClick={() => {
                setSelected(n);
                onSelect?.(n);
              }}
            >
              <circle r={isCenter ? 11 : n.kind === "customer" ? 8 : 6} fill={COLORS[n.kind] ?? "#94a3b8"} stroke={flagged ? "#dc2626" : hl.has(n.id) ? "#facc15" : "#fff"} strokeWidth={flagged || hl.has(n.id) ? 3 : 1.5} />
              {(isCenter || n.kind === "customer" || n.kind === "device") && (
                <text x={10} y={4} className="node-label">
                  {n.label.length > 22 ? n.label.slice(0, 22) + "…" : n.label}
                </text>
              )}
              <title>
                {n.kind}: {n.label}
              </title>
            </g>
          );
        })}
      </svg>
      {selected && (
        <div className="graph-detail">
          <strong>
            {selected.kind} · {selected.label}
          </strong>
          <code>{JSON.stringify(selected.props ?? {})}</code>
        </div>
      )}
      <p className="muted small">Red outline: customer with an open alert. Merchant and IP hubs are shown but not traversed.</p>
    </div>
  );
}
