#!/usr/bin/env python3
"""Start the FIRA application against a restored database and check that it really works.

Used after `pg_backup.py restore` (or restore-test --keep-database). Checks, against the restored database only:
  * the API reports ready, with the Alembic revision this build expects
  * alerts, cases, ingestion batches and audit rows are readable and counts equal the figures you pass in (optional)
  * the audit trail is still append-only (an UPDATE is refused by the database trigger)
  * a new audit event can be written (the database accepts writes)

    python infrastructure/scripts/verify_restored_app.py --url postgresql://fira:PASSWORD@localhost:5432/fira_restored
    python infrastructure/scripts/verify_restored_app.py --url ... --expect-alerts 94 --expect-cases 1

Exit code 0 only if every check passes. Run from the repository root with the backend virtual environment.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="URL of the RESTORED database")
    ap.add_argument("--expect-alerts", type=int)
    ap.add_argument("--expect-cases", type=int)
    ap.add_argument("--expect-batches", type=int)
    a = ap.parse_args()

    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from app.config import Settings
    from app.data.store import utcnow
    from app.main import create_app
    from app.monitoring.repository import AlertFilter, CaseFilter
    from app.schemas.domain import AuditEvent
    from app.services.container import build_container

    settings = Settings(environment="test", data_backend="postgres", database_url=a.url, graph_backend="networkx",
                        vector_backend="memory", llm_provider="none", model_dir=Path(tempfile.mkdtemp(prefix="fira-vr-")))
    c = build_container(settings, load_ml=False)
    checks: dict[str, object] = {}
    with TestClient(create_app(c)) as client:
        ready = client.get("/health/ready")
        body = ready.json()
        checks["ready"] = ready.status_code == 200 and body["status"] == "ready"
        checks["schema_revision"] = body["checks"].get("schema_revision")
        checks["schema_current"] = bool(body["checks"].get("schema_current"))
    _, n_alerts = c.monitoring_repo.list_alerts(AlertFilter(limit=1))
    _, n_cases = c.monitoring_repo.list_cases(CaseFilter(limit=1))
    _, n_batches = c.monitoring_repo.list_batches(limit=1)
    checks.update(alerts=n_alerts, cases=n_cases, ingestion_batches=n_batches,
                  customers=len(c.store.list_customer_ids()))
    for key, want, got in (("alerts_match", a.expect_alerts, n_alerts), ("cases_match", a.expect_cases, n_cases),
                           ("batches_match", a.expect_batches, n_batches)):
        if want is not None:
            checks[key] = want == got
    with c.store.engine.connect() as conn:  # type: ignore[attr-defined]
        try:
            conn.execute(text("UPDATE audit_log SET result = 'tampered' WHERE id = (SELECT min(id) FROM audit_log)"))
            conn.commit()
            checks["audit_append_only"] = False
        except Exception:
            conn.rollback()
            checks["audit_append_only"] = True
    before = len(c.store.list_audit(limit=5000))
    c.store.append_audit(AuditEvent(ts=utcnow(), user_id="system", role="system", action="restore_verification",
                                    result="ok", details={"database": "restored"}))
    checks["database_accepts_writes"] = len(c.store.list_audit(limit=5000)) == before + 1
    c.store.engine.dispose()  # type: ignore[attr-defined]
    passed = all(v is True for k, v in checks.items() if isinstance(v, bool))
    print(json.dumps({"passed": passed, **checks}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
