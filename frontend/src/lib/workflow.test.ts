import { describe, expect, it } from "vitest";
import {
  ALERT_STATUSES,
  ALERT_TRANSITIONS,
  buildAlertQuery,
  caseActions,
  formatHours,
  needsReason,
  filtersFromQuery,
  priorityKind,
  PRIORITIES,
  ratio,
  nextAlertStatuses,
  pageCount,
  percent,
  reasonOk,
} from "./workflow";

describe("alert state machine (mirrors backend lifecycle.py)", () => {
  it("allows the documented forward transitions", () => {
    expect(nextAlertStatuses("NEW")).toEqual(["TRIAGED", "INVESTIGATING", "ESCALATED", "RESOLVED"]);
    expect(nextAlertStatuses("INVESTIGATING")).toEqual(["ESCALATED", "RESOLVED"]);
    expect(nextAlertStatuses("ESCALATED")).toContain("INVESTIGATING");
  });
  it("treats RESOLVED as terminal and never goes backwards", () => {
    expect(nextAlertStatuses("RESOLVED")).toEqual([]);
    expect(nextAlertStatuses("INVESTIGATING")).not.toContain("TRIAGED");
    expect(nextAlertStatuses("TRIAGED")).not.toContain("NEW");
  });
  it("returns nothing for unknown statuses", () => {
    expect(nextAlertStatuses("BOGUS")).toEqual([]);
  });
  it("covers every status", () => {
    expect(Object.keys(ALERT_TRANSITIONS).sort()).toEqual([...ALERT_STATUSES].sort());
  });
  it("requires a reason to escalate or resolve, of at least five characters", () => {
    expect(needsReason("ESCALATED")).toBe(true);
    expect(needsReason("RESOLVED")).toBe(true);
    expect(needsReason("TRIAGED")).toBe(false);
    expect(reasonOk("abcd")).toBe(false);
    expect(reasonOk("  abcd  ")).toBe(false);
    expect(reasonOk("abcde")).toBe(true);
    expect(reasonOk(undefined)).toBe(false);
  });
});

describe("case actions", () => {
  it("cannot decide before the investigation starts", () => {
    expect(caseActions("OPEN")).toEqual({ transitions: ["INVESTIGATING"], decisions: [] });
  });
  it("offers closing decisions and escalation while investigating", () => {
    const a = caseActions("INVESTIGATING");
    expect(a.decisions).toEqual(["CLEARED", "FALSE_POSITIVE", "CONFIRMED_SUSPICIOUS", "ESCALATED"]);
    expect(a.transitions).toEqual(["PENDING_REVIEW"]);
  });
  it("does not offer escalation again once escalated", () => {
    expect(caseActions("ESCALATED").decisions).not.toContain("ESCALATED");
  });
  it("offers nothing on a closed case", () => {
    expect(caseActions("CLOSED")).toEqual({ transitions: [], decisions: [] });
  });
});

describe("buildAlertQuery", () => {
  it("omits empty filters and always paginates", () => {
    const q = buildAlertQuery({});
    expect(q).toContain("sort=triggered_at");
    expect(q).toContain("order=desc");
    expect(q).toContain("limit=25");
    expect(q).toContain("offset=0");
    expect(q).not.toContain("status=");
    expect(q).not.toContain("customer_id=");
  });
  it("encodes multi-value filters, upper-cases the customer and computes the offset", () => {
    const q = buildAlertQuery({ status: ["NEW", "TRIAGED"], severity: ["high"], customer: " cust-12 ", page: 3, pageSize: 10, minRisk: "40", q: "a&b=c" });
    expect(q).toContain("status=NEW%2CTRIAGED");
    expect(q).toContain("severity=high");
    expect(q).toContain("customer_id=CUST-12");
    expect(q).toContain("offset=20");
    expect(q).toContain("min_risk=40");
    expect(q).toContain("q=a%26b%3Dc");
  });
  it("never lets the offset go negative", () => {
    expect(buildAlertQuery({ page: -4 })).toContain("offset=0");
  });
});

describe("triage priority helpers", () => {
  it("lists the four priorities from most to least urgent", () => {
    expect([...PRIORITIES]).toEqual(["CRITICAL", "HIGH", "MEDIUM", "LOW"]);
  });
  it("maps priorities to badge kinds and defaults unknown values to low", () => {
    expect(priorityKind("CRITICAL")).toBe("critical");
    expect(priorityKind("HIGH")).toBe("high");
    expect(priorityKind(null)).toBe("low");
  });
  it("adds the priority filter to the alert query", () => {
    expect(buildAlertQuery({ priority: ["CRITICAL", "HIGH"], sort: "triage_score" })).toContain("priority=CRITICAL%2CHIGH");
    expect(buildAlertQuery({ priority: ["CRITICAL"], sort: "triage_score" })).toContain("sort=triage_score");
  });
  it("reads initial filters from a route query and ignores invalid values", () => {
    expect(filtersFromQuery({ priority: "CRITICAL,HIGH,URGENT", assigned: "me", status: "NEW,BOGUS" })).toEqual({
      priority: ["CRITICAL", "HIGH"],
      assignedTo: "me",
      status: ["NEW"],
    });
    expect(filtersFromQuery({ assigned: "someone-else" })).toEqual({});
  });
  it("shows n/a instead of 0% when the denominator is unknown", () => {
    expect(ratio(5, 10)).toBe("50.0%");
    expect(ratio(0, 10)).toBe("0.0%");
    expect(ratio(5, 0)).toBe("n/a");
    expect(ratio(null, 10)).toBe("n/a");
    expect(ratio(5, null)).toBe("n/a");
  });
});

describe("formatting helpers", () => {
  it("counts pages", () => {
    expect(pageCount(0, 25)).toBe(1);
    expect(pageCount(26, 25)).toBe(2);
    expect(pageCount(50, 25)).toBe(2);
  });
  it("formats durations and ratios and shows a dash for missing data", () => {
    expect(formatHours(null)).toBe("—");
    expect(formatHours(0.5)).toBe("30 min");
    expect(formatHours(5)).toBe("5.0 h");
    expect(formatHours(72)).toBe("3.0 d");
    expect(percent(0.1234)).toBe("12.3%");
    expect(percent(null)).toBe("—");
  });
});
