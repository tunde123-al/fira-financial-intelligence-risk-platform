"""Render every statement in app.data.sql_store.SQL with sample literals and run it
through `psql` (EXPLAIN for reads, inside a rolled-back transaction for writes).

Used to validate the SQL layer in environments without a Python PostgreSQL
driver. Usage: python infrastructure/scripts/verify_sql_with_psql.py "<psql args>"
"""
import json
import re
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, "backend")
from app.data.sql_store import SQL as STORE_SQL  # noqa: E402
from app.retrieval.repository import DOC_SQL  # noqa: E402

SQL = {**STORE_SQL, **{f"doc_{k}": v for k, v in DOC_SQL.items()}}

PARAMS = {
    "id": "CUST-10001", "ids": ["ACC-200001", "ACC-200002"], "start": datetime(2026, 1, 1), "end": datetime(2026, 12, 1),
    "limit": 5, "segment": "mass", "prefix": "CUST-1000%", "contains": "%alpha%", "exact": "TXN-00000001",
    "entity_id": "", "status": "", "before": datetime(2999, 1, 1), "all_subjects": True, "subjects": [],
    "all": True, "user_id": "", "action": "", "username": "admin",
    "investigation_id": "INV-TEST1", "subject_id": "CUST-10001", "subject_type": "customer", "assigned_to": None,
    "created_at": datetime(2026, 10, 1), "closed_at": None, "conclusion": None, "request_text": "x",
    "risk_score": 10.0, "created_by": "admin", "signals": ["A", "B"], "summary": "s", "report": json.dumps({"a": 1}),
    "evidence_id": "EV-1", "ref": "E1", "source_type": "database", "source_id": "CUST-10001",
    "evidence_type": "customer_profile", "title": "t", "content": json.dumps({"x": 1}), "confidence": 0.9,
    "episode_id": "EP-1", "request": "r", "requested_by": "admin", "plan": "[]", "tool_calls": "[]",
    "observations": "[]", "retrieved_evidence": "[]", "reasoning_summary": "s", "final_output": None,
    "evaluation": None, "human_feedback": None, "model": None, "tokens_in": 0, "tokens_out": 0, "latency_ms": 5,
    "started_at": datetime(2026, 10, 1), "finished_at": None, "decision_id": "D-1", "decided_by": "a",
    "decision": "confirm", "rationale": "r", "failure_categories": [], "report_quality": 4, "ts": datetime(2026, 10, 1),
    "role": "admin", "tool": None, "entity_type": None, "result": "ok", "request_id": "rq",
    "details": "{}", "version_id": "V1", "config": "{}", "approved_by": None, "approved_at": None, "run_id": "R1",
    "kind": "risk", "config_version": "v", "metrics": "{}", "password_hash": "h", "active": True,
    "document_id": "DOC-T", "doc_type": "aml_policy", "source": "s", "jurisdiction": "internal", "version": "1",
    "sha256": "a" * 64, "page_count": 1, "metadata": "{}", "chunk_id": "DOC-T#000", "ordinal": 0, "page": 1,
    "section": "Intro", "text": "funds moved rapidly through the account", "query": "rapid funds",
    "tsquery": "rapid | fund", "doc_types_all": True, "doc_types": [], "k": 5,
}


def lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, datetime):
        return f"'{v.isoformat()}+00'::timestamptz"
    if isinstance(v, list):
        return "ARRAY[" + ",".join(lit(x) for x in v) + "]::text[]" if v else "'{}'::text[]"
    return "'" + str(v).replace("'", "''") + "'"


def render(sql, **over):
    params = {**PARAMS, **over}
    return re.sub(r"(?<![:\w\\]):(\w+)(?!:)", lambda m: lit(params[m.group(1)]), sql)


def main():
    psql = sys.argv[1].split()
    failures = 0
    order = ["insert_investigation", "insert_evidence", "insert_episode", "insert_decision", "doc_upsert_doc",
             "doc_insert_chunk"]
    script = ["BEGIN;"]
    for k in order:
        script.append(render(SQL[k], status="open") + ";")
    for k, sql in SQL.items():
        if k in order:
            continue
        stmt = render(sql, status="proposed") if k == "insert_config" else render(sql)
        script.append(f"\\echo == {k}")
        script.append(("EXPLAIN " if stmt.lstrip().upper().startswith("SELECT") else "") + stmt + ";")
    script.append("\\echo == keyword search returns the inserted chunk")
    script.append(render("SELECT chunk_id FROM document_chunks c, to_tsquery('english', :tsquery) q WHERE c.tsv @@ q") + ";")
    script.append("ROLLBACK;")
    p = subprocess.run(psql + ["-v", "ON_ERROR_STOP=1", "-q"], input="\n".join(script), text=True, capture_output=True)
    if p.returncode != 0:
        failures += 1
        print(p.stdout[-2000:], p.stderr[-2000:])
    else:
        print(f"OK: {len(SQL)} statements validated")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
