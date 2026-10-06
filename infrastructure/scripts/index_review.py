#!/usr/bin/env python3
"""Measure whether each candidate index is actually used and what it saves, on synthetic data.

Creates a scratch database on the server you point it at, applies migrations 0001-0003 (the schema WITHOUT the
candidate indexes), loads N synthetic alerts and cases with a realistic skew (most alerts resolved, a minority open and
assigned), then runs the queries the application issues, with EXPLAIN (ANALYZE), before and after each candidate index is
created. Reports the plan node used and the median execution time. An index that no query uses is reported as unused.

    python infrastructure/scripts/index_review.py --url postgresql+psycopg://user:pw@localhost:5432/postgres \\
        --alerts 300000 --cases 100000 --out docs/benchmarks/index_review

Synthetic rows say nothing about a real alert population. The result is "does the planner use this index for this query
and how much faster is it on this data", which is the question the migration needs answered.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "backend" / "app" / "db"
OPEN = "('NEW','TRIAGED','INVESTIGATING','ESCALATED')"

QUERIES: dict[str, str] = {
    "my_open_alerts_by_triage": (
        "SELECT alert_id FROM monitoring_alerts WHERE status = ANY(ARRAY['NEW','TRIAGED','INVESTIGATING','ESCALATED']) "
        "AND assigned_to = 'U-user7' ORDER BY triage_score DESC NULLS LAST, alert_id LIMIT 500"),
    "queue_top_by_triage": (
        "SELECT alert_id FROM monitoring_alerts WHERE status = ANY(ARRAY['NEW','TRIAGED','INVESTIGATING','ESCALATED']) "
        "ORDER BY triage_score DESC NULLS LAST, alert_id LIMIT 50"),
    "queue_top_critical_high": (
        "SELECT alert_id FROM monitoring_alerts WHERE triage_priority = ANY(ARRAY['CRITICAL','HIGH']) "
        "AND status = ANY(ARRAY['NEW','TRIAGED','INVESTIGATING','ESCALATED']) "
        "ORDER BY triage_score DESC NULLS LAST, alert_id LIMIT 50"),
    "recently_resolved": (
        "SELECT alert_id FROM monitoring_alerts WHERE status = 'RESOLVED' "
        "AND resolved_at >= now() - interval '7 days' ORDER BY resolved_at DESC LIMIT 500"),
    "my_open_cases": (
        "SELECT case_id FROM cases WHERE assigned_to = 'U-user7' "
        "AND status = ANY(ARRAY['OPEN','INVESTIGATING','ESCALATED','PENDING_REVIEW']) LIMIT 200"),
}

CANDIDATES: dict[str, str] = {
    "ix_malerts_assignee_triage": "CREATE INDEX ix_malerts_assignee_triage ON monitoring_alerts (assigned_to, triage_score DESC NULLS LAST) "
                                  "WHERE status <> 'RESOLVED'",
    "ix_malerts_open_triage": "CREATE INDEX ix_malerts_open_triage ON monitoring_alerts (triage_score DESC NULLS LAST) "
                              "WHERE status <> 'RESOLVED'",
    "ix_malerts_open_triage_all": "CREATE INDEX ix_malerts_open_triage_all ON monitoring_alerts (triage_score DESC NULLS LAST, alert_id)",
    "ix_malerts_resolved_at": "CREATE INDEX ix_malerts_resolved_at ON monitoring_alerts (resolved_at DESC) WHERE status = 'RESOLVED'",
    "ix_cases_assigned_status": "CREATE INDEX ix_cases_assigned_status ON cases (assigned_to, status)",
}


def explain(conn: Any, sql: str, repeat: int = 7) -> dict[str, Any]:
    times, plan_nodes = [], set()
    for _ in range(repeat):
        raw = conn.execute(text("EXPLAIN (ANALYZE, FORMAT JSON) " + sql)).scalar()
        doc = raw[0] if isinstance(raw, list) else json.loads(raw)[0]
        times.append(float(doc["Execution Time"]))

        def walk(n: dict[str, Any]) -> None:
            plan_nodes.add(n["Node Type"] + (f" using {n['Index Name']}" if "Index Name" in n else ""))
            for c in n.get("Plans", []):
                walk(c)

        walk(doc["Plan"])
    return {"median_ms": round(statistics.median(times), 3), "plan": sorted(plan_nodes)}


def build(conn: Any, n_alerts: int, n_cases: int) -> None:
    for f in ("schema.sql", "schema_audit_guard.sql", "schema_monitoring.sql"):
        conn.exec_driver_sql((DB / f).read_text(encoding="utf-8"))
    # production columns without their indexes (the candidates are created one by one below)
    conn.execute(text("ALTER TABLE monitoring_alerts ADD COLUMN triage_score double precision, ADD COLUMN triage_priority text"))
    for t, c in (("monitoring_alerts", "run_id"), ("monitoring_alerts", "customer_id"), ("monitoring_alerts", "account_id"),
                 ("monitoring_alerts", "transaction_id"), ("cases", "customer_id"), ("cases", "investigation_id")):
        conn.execute(text(f"ALTER TABLE {t} DROP CONSTRAINT IF EXISTS {t}_{c}_fkey"))  # noqa: S608
    conn.execute(text(f"""
        INSERT INTO monitoring_alerts (alert_id, customer_id, detector_id, alert_type, category, severity, risk_score,
            risk_contribution, status, description, triggered_at, created_at, updated_at, last_seen_at, assigned_to,
            resolution, resolution_reason, resolved_at, resolved_by, triage_score, triage_priority)
        SELECT 'MAL-' || upper(lpad(to_hex(g), 12, '0')), 'CUST-' || g, 'DET_' || (g % 15), 'T', 'c',
               (ARRAY['low','medium','high','critical'])[1 + g % 4], (g % 100), 5, s.status, 'synthetic',
               now() - (g % 90) * interval '1 day', now(), now(), now(),
               CASE WHEN s.status <> 'NEW' OR g % 3 = 0 THEN 'U-user' || (g % 20) END,
               CASE WHEN s.status = 'RESOLVED' THEN 'CLEARED' END,
               CASE WHEN s.status = 'RESOLVED' THEN 'synthetic' END,
               CASE WHEN s.status = 'RESOLVED' THEN now() - (g % 90) * interval '1 day' END,
               CASE WHEN s.status = 'RESOLVED' THEN 'U-user1' END,
               (g * 7919 % 10000) / 100.0,
               (ARRAY['LOW','MEDIUM','HIGH','CRITICAL'])[1 + (g * 7919 % 10000) / 2500]
        FROM generate_series(1::bigint, {n_alerts}) g,
             LATERAL (SELECT CASE WHEN g % 100 < 63 THEN 'RESOLVED' WHEN g % 100 < 88 THEN 'NEW' WHEN g % 100 < 93 THEN 'TRIAGED'
                                  WHEN g % 100 < 98 THEN 'INVESTIGATING' ELSE 'ESCALATED' END AS status) s
    """))
    conn.execute(text(f"""
        INSERT INTO cases (case_id, case_number, customer_id, status, priority, title, assigned_to, opened_at, created_at,
                           updated_at, closed_at, decision)
        SELECT 'CASE-' || upper(lpad(to_hex(g), 12, '0')), 'FC-' || g, 'CUST-' || (10000 + g), s.status, 'medium', 't',
               'U-user' || (g % 20), now(), now(), now(),
               CASE WHEN s.status = 'CLOSED' THEN now() END, CASE WHEN s.status = 'CLOSED' THEN 'CLEARED' END
        FROM generate_series(1::bigint, {n_cases}) g,
             LATERAL (SELECT CASE WHEN g % 100 < 92 THEN 'CLOSED' WHEN g % 100 < 96 THEN 'OPEN' ELSE 'INVESTIGATING' END AS status) s
    """))
    conn.execute(text("ANALYZE monitoring_alerts"))
    conn.execute(text("ANALYZE cases"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="server URL (a scratch database is created and dropped)")
    ap.add_argument("--alerts", type=int, default=300_000)
    ap.add_argument("--cases", type=int, default=100_000)
    ap.add_argument("--out", type=Path, required=True, help="output path without extension")
    a = ap.parse_args()
    url = make_url(a.url)
    name = f"fira_idx_{uuid.uuid4().hex[:8]}"
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))  # noqa: S608
    results: dict[str, Any] = {"created_at": datetime.now(timezone.utc).isoformat(), "alerts": a.alerts, "cases": a.cases,
                               "synthetic": True, "queries": {}, "candidates": {}}
    try:
        eng = create_engine(url.set(database=name), isolation_level="AUTOCOMMIT")
        with eng.connect() as conn:
            conn.execute(text("SET max_parallel_workers_per_gather = 0"))  # steady, comparable plans
            build(conn, a.alerts, a.cases)
            base = {q: explain(conn, sql) for q, sql in QUERIES.items()}
            results["baseline"] = base
            for idx, ddl in CANDIDATES.items():
                conn.execute(text(ddl))
                conn.execute(text("ANALYZE monitoring_alerts"))
                conn.execute(text("ANALYZE cases"))
                after = {q: explain(conn, sql) for q, sql in QUERIES.items()}
                used_by = [q for q, r in after.items() if any(idx in p for p in r["plan"])]
                results["candidates"][idx] = {
                    "ddl": ddl, "used_by": used_by, "unused": not used_by,
                    "per_query": {q: {"before_ms": base[q]["median_ms"], "after_ms": after[q]["median_ms"],
                                      "plan_after": after[q]["plan"]} for q in QUERIES},
                    "size_mb": round(conn.execute(text("SELECT pg_relation_size(CAST(:i AS regclass))"), {"i": idx}).scalar() / 2 ** 20, 2)}
                conn.execute(text(f"DROP INDEX {idx}"))  # noqa: S608
            # the indexes the migration ships, created together: the planner must still pick the right one per query
            shipped = ["ix_malerts_assignee_triage", "ix_malerts_open_triage", "ix_malerts_resolved_at",
                       "ix_cases_assigned_status"]
            for idx in shipped:
                conn.execute(text(CANDIDATES[idx]))
            conn.execute(text("ANALYZE monitoring_alerts"))
            conn.execute(text("ANALYZE cases"))
            results["shipped_together"] = {q: explain(conn, sql) for q, sql in QUERIES.items()}
        eng.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))  # noqa: S608
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.with_suffix(".json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    lines = [f"# Index review ({results['created_at'][:10]})", "",
             f"Synthetic data: {a.alerts:,} alerts (63% resolved, 37% open, ~2/3 of open assigned across 20 users) and {a.cases:,} cases "
             f"(92% closed). Median of 7 runs of `EXPLAIN (ANALYZE)`, parallel workers off. Each candidate was tested alone against the "
             "schema without it.", "",
             "| candidate | used by | size MB | " + " | ".join(QUERIES) + " |", "|---|---|---|" + "---|" * len(QUERIES)]
    for idx, r in results["candidates"].items():
        cells = []
        for q in QUERIES:
            pq = r["per_query"][q]
            cells.append(f"{pq['before_ms']} -> {pq['after_ms']} ms" + (" (used)" if q in r["used_by"] else ""))
        lines.append(f"| {idx} | {', '.join(r['used_by']) or '**unused**'} | {r['size_mb']} | " + " | ".join(cells) + " |")
    lines += ["", f"Shipped set together ({', '.join(['ix_malerts_assignee_triage', 'ix_malerts_open_triage', 'ix_malerts_resolved_at', 'ix_cases_assigned_status'])}):", ""]
    for q, r in results["shipped_together"].items():
        lines.append(f"- {q}: {results['baseline'][q]['median_ms']} -> {r['median_ms']} ms, {'; '.join(r['plan'])}")
    lines += ["", "Baseline plans (no candidate index):", ""]
    for q, r in results["baseline"].items():
        lines.append(f"- {q}: {r['median_ms']} ms, {'; '.join(r['plan'])}")
    a.out.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
