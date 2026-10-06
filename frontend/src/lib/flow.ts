// Layout for the money-flow view. Pure functions so the arrangement can be tested without a browser.
// Columns run left to right: upstream accounts (money coming in), the customer's own accounts, downstream accounts.

export interface FlowNode {
  account_id: string;
  owner_customer_id: string | null;
  depth: number;
  roles: string[];
  flagged: boolean;
  is_hub: boolean;
  in_degree: number;
  out_degree: number;
}

export interface FlowEdge {
  source: string;
  target: string;
  total_usd: number;
  n: number;
  first_ts: string | null;
  last_ts: string | null;
  depth: number;
  direction: "in" | "out";
  sample_txn_ids: string[];
}

export interface Placed extends FlowNode {
  x: number;
  y: number;
  column: number;
}

export const COL_W = 210;
export const ROW_H = 54;
export const PAD = 36;

/** Column of a node: 0 for the subject's accounts, -depth upstream, +depth downstream. */
export function columnOf(n: FlowNode): number {
  if (n.depth === 0) return 0;
  const down = n.roles.includes("downstream");
  const up = n.roles.includes("upstream");
  if (down && !up) return n.depth;
  if (up && !down) return -n.depth;
  return n.depth; // both directions at the same depth: place downstream, the edge table shows both
}

export function layoutFlow(nodes: FlowNode[]): { placed: Placed[]; width: number; height: number } {
  const cols = new Map<number, FlowNode[]>();
  for (const n of nodes) {
    const c = columnOf(n);
    cols.set(c, [...(cols.get(c) ?? []), n]);
  }
  const keys = [...cols.keys()].sort((a, b) => a - b);
  const minCol = keys.length ? keys[0] : 0;
  const placed: Placed[] = [];
  let maxRows = 1;
  for (const k of keys) {
    const list = (cols.get(k) ?? []).slice().sort((a, b) => Number(b.flagged) - Number(a.flagged) || a.account_id.localeCompare(b.account_id));
    maxRows = Math.max(maxRows, list.length);
    list.forEach((n, i) => placed.push({ ...n, column: k, x: PAD + (k - minCol) * COL_W, y: PAD + i * ROW_H }));
  }
  const width = PAD * 2 + Math.max(1, keys.length) * COL_W - (COL_W - 120);
  return { placed, width: Math.max(width, 320), height: PAD * 2 + maxRows * ROW_H };
}

/** Stroke width for an edge, growing with the logarithm of the amount (1.5 to 7 px). */
export function strokeFor(usd: number): number {
  return Math.min(7, Math.max(1.5, Math.log10(Math.max(usd, 1)) * 1.4 - 1.5));
}

export function usd(v: number): string {
  return v >= 1000 ? `$${(v / 1000).toFixed(v >= 10000 ? 0 : 1)}k` : `$${v.toFixed(0)}`;
}
