import { ReactNode, useEffect, useState } from "react";
import { ApiError } from "../api";

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): { data: T | null; error: string | null; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    fn()
      .then((d) => alive && setData(d))
      .catch((e: unknown) => alive && setError(errorText(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return { data, error, loading, reload: () => setTick((t) => t + 1) };
}

export function errorText(e: unknown): string {
  if (e instanceof ApiError) return `${e.status}: ${e.message}${e.requestId ? ` (request ${e.requestId})` : ""}`;
  if (e instanceof Error) return e.message;
  return String(e);
}

export function Card({ title, actions, children }: { title?: ReactNode; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="card">
      {(title || actions) && (
        <header className="card-head">
          <h3>{title}</h3>
          <div>{actions}</div>
        </header>
      )}
      <div className="card-body">{children}</div>
    </section>
  );
}

export function Status({ loading, error }: { loading: boolean; error: string | null }) {
  if (loading) return <div className="muted">Loading…</div>;
  if (error) return <div className="error">{error}</div>;
  return null;
}

const SEV: Record<string, string> = { low: "sev-low", medium: "sev-med", high: "sev-high", critical: "sev-crit" };
export function Badge({ value, kind }: { value: ReactNode; kind?: string }) {
  return <span className={`badge ${kind ? SEV[kind] ?? `b-${kind}` : ""}`}>{value}</span>;
}

export function ScoreBadge({ score, band }: { score: number | null | undefined; band?: string }) {
  if (score === null || score === undefined) return <span className="muted">—</span>;
  const b = band ?? (score >= 75 ? "critical" : score >= 50 ? "high" : score >= 25 ? "medium" : "low");
  return <Badge value={`${score.toFixed(1)} · ${b}`} kind={b} />;
}

export interface Column<T> {
  key: string;
  label: string;
  render?: (row: T) => ReactNode;
  align?: "left" | "right";
}

export function Table<T extends Record<string, any>>({ rows, columns, empty = "No rows" }: { rows: T[]; columns: Column<T>[]; empty?: string }) {
  if (!rows.length) return <div className="muted">{empty}</div>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key} style={{ textAlign: c.align ?? "left" }}>
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {columns.map((c) => (
                <td key={c.key} style={{ textAlign: c.align ?? "left" }}>
                  {c.render ? c.render(r) : fmt(r[c.key])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function fmt(v: unknown): ReactNode {
  if (v === null || v === undefined || v === "") return <span className="muted">—</span>;
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (Array.isArray(v)) return v.join(", ");
  if (typeof v === "object") return <code>{JSON.stringify(v)}</code>;
  const s = String(v);
  if (/^\d{4}-\d{2}-\d{2}T/.test(s)) return s.replace("T", " ").slice(0, 16);
  return s;
}

export function Bars({ data, max }: { data: { label: string; value: number; note?: string }[]; max?: number }) {
  const m = max ?? Math.max(1, ...data.map((d) => d.value));
  return (
    <div className="bars">
      {data.map((d) => (
        <div className="bar-row" key={d.label}>
          <span className="bar-label">{d.label}</span>
          <span className="bar-track">
            <span className="bar-fill" style={{ width: `${Math.min(100, (d.value / m) * 100)}%` }} />
          </span>
          <span className="bar-value">
            {d.value.toLocaleString(undefined, { maximumFractionDigits: 2 })}
            {d.note ? ` ${d.note}` : ""}
          </span>
        </div>
      ))}
    </div>
  );
}

export function Kpi({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="kpi">
      <div className="kpi-value">{value}</div>
      <div className="kpi-label">{label}</div>
    </div>
  );
}

export function JsonBlock({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}

export function Tabs({ tabs, current, onChange }: { tabs: string[]; current: string; onChange: (t: string) => void }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button key={t} role="tab" aria-selected={t === current} className={t === current ? "tab active" : "tab"} onClick={() => onChange(t)}>
          {t}
        </button>
      ))}
    </div>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
    </label>
  );
}

export const Spacer = () => <div style={{ height: 12 }} />;
