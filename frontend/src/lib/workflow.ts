// Pure helpers for the alert and case workflow. They mirror backend/app/monitoring/lifecycle.py so the UI
// only offers actions the API will accept; the API remains the authority and re-validates everything.

export const ALERT_STATUSES = ["NEW", "TRIAGED", "INVESTIGATING", "ESCALATED", "RESOLVED"] as const;
export type AlertStatus = (typeof ALERT_STATUSES)[number];
export const RESOLUTIONS = ["CLEARED", "FALSE_POSITIVE", "CONFIRMED_SUSPICIOUS"] as const;
export type Resolution = (typeof RESOLUTIONS)[number];

export const ALERT_TRANSITIONS: Record<AlertStatus, AlertStatus[]> = {
  NEW: ["TRIAGED", "INVESTIGATING", "ESCALATED", "RESOLVED"],
  TRIAGED: ["INVESTIGATING", "ESCALATED", "RESOLVED"],
  INVESTIGATING: ["ESCALATED", "RESOLVED"],
  ESCALATED: ["INVESTIGATING", "RESOLVED"],
  RESOLVED: [],
};

export const MIN_REASON = 5;

export function nextAlertStatuses(status: string): AlertStatus[] {
  return ALERT_TRANSITIONS[status as AlertStatus] ?? [];
}

export function reasonOk(reason: string | null | undefined): boolean {
  return (reason ?? "").trim().length >= MIN_REASON;
}

/** Statuses (other than RESOLVED, which has its own form) that need a written reason. */
export function needsReason(target: string): boolean {
  return target === "ESCALATED" || target === "RESOLVED";
}

export type CaseStatus = "OPEN" | "INVESTIGATING" | "ESCALATED" | "PENDING_REVIEW" | "CLOSED";
export const CASE_DECISIONS = ["CLEARED", "FALSE_POSITIVE", "CONFIRMED_SUSPICIOUS", "ESCALATED"] as const;
export type CaseDecision = (typeof CASE_DECISIONS)[number];

export interface CaseActions {
  transitions: ("INVESTIGATING" | "PENDING_REVIEW")[];
  decisions: CaseDecision[];
}

/** What an investigator may do next on a case in `status`. */
export function caseActions(status: string): CaseActions {
  switch (status) {
    case "OPEN":
      return { transitions: ["INVESTIGATING"], decisions: [] };
    case "INVESTIGATING":
      return { transitions: ["PENDING_REVIEW"], decisions: ["CLEARED", "FALSE_POSITIVE", "CONFIRMED_SUSPICIOUS", "ESCALATED"] };
    case "ESCALATED":
      return { transitions: ["INVESTIGATING", "PENDING_REVIEW"], decisions: ["CLEARED", "FALSE_POSITIVE", "CONFIRMED_SUSPICIOUS"] };
    case "PENDING_REVIEW":
      return { transitions: ["INVESTIGATING"], decisions: ["CLEARED", "FALSE_POSITIVE", "CONFIRMED_SUSPICIOUS"] };
    default:
      return { transitions: [], decisions: [] };
  }
}

export const PRIORITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW"] as const;
export type Priority = (typeof PRIORITIES)[number];

/** Badge colour class for a triage priority (reuses the severity palette). */
export function priorityKind(p: string | null | undefined): string {
  switch (p) {
    case "CRITICAL":
      return "critical";
    case "HIGH":
      return "high";
    case "MEDIUM":
      return "medium";
    default:
      return "low";
  }
}

export interface AlertFilters {
  status?: string[];
  severity?: string[];
  priority?: string[];
  detector?: string;
  minRisk?: string;
  maxRisk?: string;
  customer?: string;
  assignedTo?: string; // "", "me", "unassigned"
  q?: string;
  dateFrom?: string;
  dateTo?: string;
  sort?: string;
  order?: "asc" | "desc";
  page?: number;
  pageSize?: number;
}

/** Query string for GET /api/monitoring/alerts. Empty values are omitted. */
export function buildAlertQuery(f: AlertFilters): string {
  const pageSize = f.pageSize ?? 25;
  const params: Record<string, string> = {};
  const put = (k: string, v: string | undefined | null) => {
    if (v !== undefined && v !== null && String(v).trim() !== "") params[k] = String(v).trim();
  };
  if (f.status?.length) put("status", f.status.join(","));
  if (f.severity?.length) put("severity", f.severity.join(","));
  if (f.priority?.length) put("priority", f.priority.join(","));
  put("detector", f.detector);
  put("min_risk", f.minRisk);
  put("max_risk", f.maxRisk);
  put("customer_id", f.customer?.toUpperCase());
  put("assigned_to", f.assignedTo);
  put("q", f.q);
  put("date_from", f.dateFrom);
  put("date_to", f.dateTo);
  put("sort", f.sort ?? "triggered_at");
  put("order", f.order ?? "desc");
  params["limit"] = String(pageSize);
  params["offset"] = String(Math.max(0, ((f.page ?? 1) - 1) * pageSize));
  return "?" + Object.entries(params).map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&");
}

/** Initial alert filters from a hash-route query such as `#/alerts?assigned=me&priority=CRITICAL,HIGH`. */
export function filtersFromQuery(q: Record<string, string>): Partial<AlertFilters> {
  const out: Partial<AlertFilters> = {};
  const list = (v: string | undefined) => (v ? v.split(",").map((x) => x.trim()).filter(Boolean) : undefined);
  const pr = list(q.priority)?.filter((p) => (PRIORITIES as readonly string[]).includes(p));
  if (pr?.length) out.priority = pr;
  const st = list(q.status)?.filter((s) => (ALERT_STATUSES as readonly string[]).includes(s));
  if (st?.length) out.status = st;
  if (q.assigned === "me" || q.assigned === "unassigned") out.assignedTo = q.assigned;
  if (q.sort) out.sort = q.sort;
  return out;
}

/** Share of a total as a percentage string; "n/a" when the denominator is unknown or zero (never 0%). */
export function ratio(part: number | null | undefined, total: number | null | undefined): string {
  if (part === null || part === undefined || !total) return "n/a";
  return `${((part / total) * 100).toFixed(1)}%`;
}

export function pageCount(total: number, pageSize: number): number {
  return Math.max(1, Math.ceil(total / Math.max(1, pageSize)));
}

export function formatHours(h: number | null | undefined): string {
  if (h === null || h === undefined) return "—";
  if (h < 1) return `${Math.round(h * 60)} min`;
  if (h < 48) return `${h.toFixed(1)} h`;
  return `${(h / 24).toFixed(1)} d`;
}

export function percent(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`;
}

/** Evidence class -> short label shown in the UI. LLM text is never presented as a source of fact. */
export const EVIDENCE_LABELS: Record<string, string> = {
  DATABASE_FACT: "Database fact",
  RULE_RESULT: "Rule result",
  GRAPH_RESULT: "Graph result",
  DOCUMENT_EVIDENCE: "Document evidence",
  LLM_GENERATED_SUMMARY: "AI-generated summary",
};
