#!/usr/bin/env python3
"""End-to-end acceptance test of FIRA against a RUNNING API backed by PostgreSQL, using synthetic data only.

It walks the whole investigator and operator journey over real HTTP and checks every claim with an assertion, then writes a
Markdown/JSON report. Standard library only (plus the repository's own backup scripts via subprocess).

What it covers (each is a numbered step in the report):
  data-quality failure and accounting; monitoring; alerts with triage factors; the money-mule scenario; the false-positive
  scenario; assignment; case; investigation of customer, transactions and graph; evidence; note; priority change; invalid
  transition; decision and closure; audit trail; operations metrics; configuration governance; security behaviour (401, 403,
  413, lockout, logout revocation); backup; verify; restore test; restore into a named database; the restored application.

What it does NOT cover (run these separately, see docs/ACCEPTANCE_TEST.md): the labelled evaluation, the performance benchmark,
the unit/integration test suites and the CI static checks.

The harness reads `scenario_labels.json` from the dataset directory ONLY to pick subjects (a mule, a benign look-alike, a
normal customer) and to confirm outcomes. Product code never reads labels.

    python infrastructure/scripts/acceptance_test.py --url http://localhost:8765 --dataset data/acceptance_bank \\
        --admin-password ... --analyst-password ... --db-url postgresql://fira:PW@localhost:55432/fira_accept \\
        --docker-container fira-test-pg --out docs/acceptance_run
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RESULTS: list[dict[str, Any]] = []


class Http:
    def __init__(self, base: str):
        self.base, self.token = base.rstrip("/"), None

    def call(self, method: str, path: str, body: Any = None, token: str | None = ..., raw: bytes | None = None,  # type: ignore[assignment]
             expect: int | tuple[int, ...] = 200) -> tuple[int, Any, dict[str, str]]:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)  # noqa: S310
        req.add_header("Content-Type", "application/json")
        tok = self.token if token is ... else token
        if tok:
            req.add_header("Authorization", f"Bearer {tok}")
        try:
            with urllib.request.urlopen(req, timeout=600) as r:  # noqa: S310  # nosec B310
                status, payload, headers = r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            status, payload, headers = e.code, e.read(), dict(e.headers)
        try:
            parsed: Any = json.loads(payload) if payload and payload[:1] in (b"{", b"[") else payload.decode(errors="replace")
        except ValueError:
            parsed = payload.decode(errors="replace")
        ok = status in (expect if isinstance(expect, tuple) else (expect,))
        if not ok:
            raise AssertionError(f"{method} {path} -> HTTP {status}: {str(parsed)[:300]}")
        return status, parsed, headers

    def get(self, path: str, **kw: Any) -> Any:
        return self.call("GET", path, **kw)[1]

    def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.call("POST", path, body, **kw)[1]

    def login(self, user: str, pw: str) -> str:
        return str(self.post("/api/auth/login", {"username": user, "password": pw}, token=None)["access_token"])


def step(title: str):
    def deco(fn):
        def run(*a, **k):
            t0 = time.perf_counter()
            rec: dict[str, Any] = {"n": len(RESULTS) + 1, "step": title, "passed": False, "evidence": {}}
            try:
                rec["evidence"] = fn(*a, **k) or {}
                rec["passed"] = True
            except AssertionError as e:
                rec["error"] = str(e)[:500]
            except Exception as e:  # report unexpected failures as failures, never crash silently
                rec["error"] = f"{type(e).__name__}: {e}"[:500]
            rec["seconds"] = round(time.perf_counter() - t0, 2)
            RESULTS.append(rec)
            print(f"[{rec['n']:>2}] {'PASS' if rec['passed'] else 'FAIL'}  {title}  ({rec['seconds']} s)" + ("" if rec["passed"] else f"\n      {rec['error']}"),
                  flush=True)
            return rec["passed"]
        return run
    return deco


def load_dataset(path: Path) -> dict[str, Any]:
    labels = json.loads((path / "scenario_labels.json").read_text(encoding="utf-8"))
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    by: dict[str, list[str]] = {}
    for lb in labels:
        by.setdefault(lb["scenario"], []).append(lb["entity_id"])
    accounts: dict[str, list[dict[str, str]]] = {}
    with gzip.open(path / "accounts.csv.gz", "rt", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            accounts.setdefault(r["customer_id"], []).append(r)
    return {"by": by, "manifest": manifest, "accounts": accounts, "as_of": datetime.fromisoformat(str(manifest["as_of"]).replace("Z", "+00:00"))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8765")
    ap.add_argument("--dataset", type=Path, required=True, help="the dataset directory the API was loaded from")
    ap.add_argument("--admin-password", required=True)
    ap.add_argument("--analyst-password", required=True)
    ap.add_argument("--db-url", help="database URL of the API's database (enables the backup and restore steps)")
    ap.add_argument("--docker-container", help="run pg_dump/pg_restore inside this container")
    ap.add_argument("--out", type=Path, required=True, help="report path without extension")
    a = ap.parse_args()
    ds = load_dataset(a.dataset)
    h = Http(a.url)
    ctx: dict[str, Any] = {}
    pick = lambda sc, i=0: ds["by"][sc][i]  # noqa: E731

    @step("API is ready, schema current, baseline ingestion batch recorded from the dataset manifest")
    def s_ready():
        d = h.get("/health/ready", token=None)
        assert d["status"] == "ready", d
        assert d["checks"]["schema_current"] is True and d["checks"]["schema_revision"] == "0004", d["checks"]
        ctx["admin"], ctx["analyst"] = h.login("admin", a.admin_password), h.login("analyst", a.analyst_password)
        s = h.get("/api/data-quality/summary", token=ctx["analyst"])
        t = s["totals"]
        assert t["batches"] >= 1 and t["received"] > 0, t
        declared = ds["manifest"]["counts"]["transactions"]
        assert t["expected"] == declared, (t["expected"], declared)
        return {"schema_revision": d["checks"]["schema_revision"], "db_latency_ms": d["checks"].get("database_latency_ms"),
                "baseline_expected": t["expected"], "baseline_received": t["received"], "coverage": t["coverage"]}

    @step("Data-quality failure: a dirty batch is quarantined with reasons, accounting balances, nothing is lost silently")
    def s_dirty():
        cust = pick("normal", 0)
        acc = ds["accounts"][cust][0]["account_id"]
        end = ds["as_of"]
        ts = lambda hrs: (end - timedelta(hours=hrs)).isoformat()  # noqa: E731
        base = {"sender_account_id": acc, "currency": "USD", "transaction_type": "transfer", "channel": "web"}
        suffix = uuid.uuid4().int % 10**6
        rows: list[Any] = []
        for i in range(12):  # twelve valid rows with distinct amounts and times
            rows.append({**base, "transaction_id": f"TXN-8{suffix:06d}{i:02d}"[:16], "timestamp": ts(5 - i * 0.2), "amount": 20 + i * 7.5})
        rows.append(dict(rows[0]))  # same id again: DUPLICATE_ID
        rows.append({**base, "timestamp": "last tuesday", "amount": 10})  # INVALID_TIMESTAMP
        rows.append({**base, "timestamp": ts(1), "amount": -50})  # NON_POSITIVE_AMOUNT
        rows.append({**base, "timestamp": ts(1), "amount": 5, "sender_account_id": "ACC-99999999"})  # UNKNOWN_ACCOUNT
        rows.append({**base, "timestamp": ts(1), "amount": 5, "surprise": True})  # MALFORMED_ROW (unknown field)
        rows.append({"timestamp": ts(1), "amount": 5})  # MISSING_FIELD
        rows.append({**base, "timestamp": ts(24 * 20), "amount": 31.31})  # accepted, but LATE (20 days behind the newest row)
        r = h.post("/api/monitoring/transactions", {"transactions": rows, "run_monitoring": False, "source": "acceptance-dirty-batch",
                                                    "expected": {"count": 25}}, token=ctx["admin"])
        b = r["batch"]
        assert b["received"] == len(rows) == b["processed"] + b["rejected"] + b["failed"], b
        assert b["processed"] == 13 and b["rejected"] == 6, b
        assert b["duplicates"] == 1 and b["malformed"] == 3 and b["late"] == 1, b
        assert b["expected_count"] == 25 and b["missing"] == 25 - len(rows), b
        rej = h.get(f"/api/data-quality/rejected?batch_id={b['batch_id']}&limit=50", token=ctx["analyst"])
        codes = sorted(x["reason_code"] for x in rej["items"])
        assert codes == sorted(["DUPLICATE_ID", "INVALID_TIMESTAMP", "NON_POSITIVE_AMOUNT", "UNKNOWN_ACCOUNT", "MALFORMED_ROW", "MISSING_FIELD"]), codes
        one = h.get(f"/api/data-quality/rejected?reason_code=UNKNOWN_ACCOUNT&batch_id={b['batch_id']}", token=ctx["analyst"])
        assert one["total"] == 1 and "ACC-99999999" in json.dumps(one["items"][0]["payload"])
        t = h.get("/api/data-quality/summary", token=ctx["analyst"])["totals"]
        assert t["rejected"] >= 6 and t["late"] >= 1 and t["quality_score"] is not None
        ctx["dirty_batch"] = b["batch_id"]
        return {"batch": b["batch_id"], "accounting": f"{b['received']} = {b['processed']} + {b['rejected']} + {b['failed']}",
                "reason_codes": codes, "late": b["late"], "missing": b["missing"], "coverage_overall": t["coverage"],
                "quality_score": t["quality_score"]}

    @step("Monitoring run over the scenario customers raises alerts with explainable triage")
    def s_run():
        subjects = (ds["by"].get("mule_account", [])[:2] + ds["by"].get("mule_to_collector", [])[:2] + ds["by"].get("legit_family_collection", [])
                    + ds["by"].get("legit_marketplace_seller", [])[:2] + ds["by"].get("structuring_detectable", [])[:2]
                    + ds["by"].get("normal", [])[:10])
        run = h.post("/api/monitoring/run", {"customer_ids": subjects}, token=ctx["admin"])
        assert run["errors"] == 0 and run["alerts_created"] > 0, run
        items = h.get("/api/monitoring/alerts?limit=200&sort=triage_score&order=desc", token=ctx["analyst"])["items"]
        assert items, "no alerts"
        th = {"CRITICAL": 65, "HIGH": 50, "MEDIUM": 30}
        for al in items:
            assert len(al["triage_factors"]) == 12, al["alert_id"]
            assert abs(sum(f["points"] for f in al["triage_factors"]) - al["triage_score"]) <= 0.11, al["alert_id"]
            exp = "CRITICAL" if al["triage_score"] >= th["CRITICAL"] else "HIGH" if al["triage_score"] >= th["HIGH"] else "MEDIUM" if al["triage_score"] >= th["MEDIUM"] else "LOW"
            assert al["triage_priority"] == exp, (al["alert_id"], al["triage_score"], al["triage_priority"])
        scores = [x["triage_score"] for x in items]
        assert scores == sorted(scores, reverse=True)
        ctx["alerts"] = items
        by_p: dict[str, int] = {}
        for al in items:
            by_p[al["triage_priority"]] = by_p.get(al["triage_priority"], 0) + 1
        return {"customers_screened": run["customers_evaluated"], "alerts_created": run["alerts_created"], "duration_ms": run["duration_ms"],
                "alerts_by_priority": by_p, "top_alert": {k: items[0][k] for k in ("alert_id", "detector_id", "triage_score", "triage_priority")}}

    @step("Why a score: the triage explanation lists every factor with points, maximum and reason")
    def s_triage():
        top = ctx["alerts"][0]
        e = h.get(f"/api/monitoring/triage/{top['alert_id']}", token=ctx["analyst"])
        assert e["max_total"] == 100 and "not a probability" in e["method"], e
        assert all(f["reason"] and f["points"] <= f["max"] for f in e["factors"])
        return {"alert": top["alert_id"], "score": e["score"], "priority": e["priority"],
                "largest_factors": sorted(((f["label"], f["points"]) for f in e["factors"]), key=lambda x: -x[1])[:3]}

    @step("Money-mule scenario: indicators with evidence, flow graph, fan-in search")
    def s_mule():
        mule = pick("mule_account", 0)
        m = h.get(f"/api/mule/customers/{mule}", token=ctx["analyst"])
        fired = {i["id"] for i in m["indicators"] if i["fired"]}
        assert m["band"] in ("MEDIUM", "HIGH") and {"fan_in", "rapid_movement"} <= fired, (m["band"], fired)
        assert "not proof" in m["disclaimer"]
        assert all(i["reason"] for i in m["indicators"])
        f = h.get(f"/api/mule/customers/{mule}/flow?depth=2&days=30", token=ctx["analyst"])
        assert f["nodes"] and f["edges"] and any(n["depth"] == 0 for n in f["nodes"])
        e0 = f["edges"][0]
        assert {"source", "target", "total_usd", "n", "first_ts", "last_ts", "direction", "depth"} <= set(e0)
        pat = h.get("/api/mule/patterns?min_degree=5", token=ctx["analyst"])
        assert mule in {p["owner_customer_id"] for p in pat["patterns"]}
        ctx["mule"] = mule
        return {"customer": mule, "band": m["band"], "score": m["score"], "indicators_fired": sorted(fired),
                "flow_nodes": len(f["nodes"]), "flow_edges": len(f["edges"]), "in_usd": m["inbound_usd"], "out_usd": m["outbound_usd"]}

    @step("False-positive scenario: a benign look-alike alerts, is reviewed and cleared; feedback is recorded")
    def s_fp():
        fam = [c for c in ds["by"].get("legit_family_collection", []) if any(al["customer_id"] == c for al in ctx["alerts"])]
        assert fam, "no benign look-alike alerted in this run"
        cust = fam[0]
        al = next(x for x in ctx["alerts"] if x["customer_id"] == cust)
        m = h.get(f"/api/mule/customers/{cust}", token=ctx["analyst"])
        assert m["band"] in ("NONE", "LOW", "MEDIUM")
        h.post(f"/api/monitoring/alerts/{al['alert_id']}/assign", {"assignee": "U-analyst"}, token=ctx["analyst"])
        h.post(f"/api/monitoring/alerts/{al['alert_id']}/resolve", {"resolution": "FALSE_POSITIVE",
                                                                    "reason": "relatives pooled money for a gift; forwarded within a day"}, token=ctx["analyst"])
        fb = h.get("/api/monitoring/feedback?limit=20", token=ctx["analyst"])
        rec = next(x for x in fb if x["alert_id"] == al["alert_id"])
        assert rec["decision"] == "FALSE_POSITIVE" and rec["investigator"] == "U-analyst" and rec["reason"] and rec["decided_at"]
        return {"customer": cust, "alert": al["alert_id"], "mule_band": m["band"], "recorded": {k: rec[k] for k in ("decision", "investigator", "decided_at")}}

    @step("Investigator queue: assign, My Work ordering, invalid transitions refused")
    def s_assign():
        mule = ctx["mule"]
        mine = [x for x in ctx["alerts"] if x["customer_id"] == mule]
        assert mine, "mule has no alert"
        al = mine[0]
        h.post(f"/api/monitoring/alerts/{al['alert_id']}/assign", {"assignee": "U-analyst"}, token=ctx["analyst"])
        w = h.get("/api/monitoring/my-work", token=ctx["analyst"])
        assert al["alert_id"] in {x["alert_id"] for x in w["open_alerts"]}
        sc = [x["triage_score"] for x in w["open_alerts"]]
        assert sc == sorted(sc, reverse=True)
        assert w["counts"]["open"] == len(w["open_alerts"])
        h.call("POST", f"/api/monitoring/alerts/{al['alert_id']}/transition", {"status": "NEW"}, token=ctx["analyst"], expect=(409, 422))
        h.call("POST", f"/api/monitoring/alerts/{al['alert_id']}/assign", {"assignee": "U-admin"}, token=ctx["analyst"], expect=403)
        ctx["mule_alert"] = al
        return {"my_open": w["counts"]["open"], "high_priority": w["counts"]["high_priority"], "overdue": w["counts"]["overdue"]}

    @step("Case: create, investigate customer, transactions and graph, attach evidence, note, change priority")
    def s_case():
        al = ctx["mule_alert"]
        c = h.post("/api/cases", {"customer_id": al["customer_id"], "alert_ids": [al["alert_id"]], "priority": "medium"}, token=ctx["analyst"], expect=(200, 201))["case"]
        cid = c["case_id"]
        ctx["case"] = c
        wb = h.get(f"/api/cases/{cid}/workbench", token=ctx["analyst"])
        assert wb["timeline"] and wb["case"]["status"] == "OPEN"
        cust = h.get(f"/api/customers/{al['customer_id']}", token=ctx["analyst"])
        tx = h.get(f"/api/customers/{al['customer_id']}/transactions?lookback_days=90&limit=50", token=ctx["analyst"])
        assert cust and tx
        cp = h.get(f"/api/network/customers/{al['customer_id']}/counterparties?degree=2&days=90", token=ctx["analyst"])
        assert cp["counterparties"]
        txn_id = al["transaction_ids"][0] if al.get("transaction_ids") else None
        assert txn_id, "alert has no transaction evidence"
        h.post(f"/api/cases/{cid}/evidence", {"kind": "transaction", "ref": txn_id, "note": "first inbound transfer of the pattern"}, token=ctx["analyst"], expect=(200, 201))
        h.call("POST", f"/api/cases/{cid}/evidence", {"kind": "transaction", "ref": "TXN-000000000000"}, token=ctx["analyst"], expect=(404, 409, 422))
        h.post(f"/api/cases/{cid}/notes", {"body": "Many small inbound transfers forwarded within hours; checking counterparties."}, token=ctx["analyst"], expect=(200, 201))
        h.post(f"/api/cases/{cid}/priority", {"priority": "high", "reason": "fast onward movement confirmed in the flow view"}, token=ctx["analyst"])
        h.call("POST", f"/api/cases/{cid}/transition", {"status": "PENDING_REVIEW"}, token=ctx["analyst"], expect=409)  # OPEN -> PENDING_REVIEW is invalid
        h.post(f"/api/cases/{cid}/transition", {"status": "INVESTIGATING"}, token=ctx["analyst"])
        ev = h.get(f"/api/cases/{cid}/evidence", token=ctx["analyst"])
        assert any(e.get("ref") == txn_id or txn_id in json.dumps(e) for e in (ev if isinstance(ev, list) else ev.get("items", [])))
        return {"case": c["case_number"], "counterparties": len(cp["counterparties"]), "customer_transactions": len(tx.get("transactions", tx)) if isinstance(tx, dict) else len(tx),
                "evidence_transaction": txn_id}

    @step("Decision closes the case, resolves its alerts and appears in the timeline in real-time order")
    def s_decide():
        cid = ctx["case"]["case_id"]
        h.post(f"/api/cases/{cid}/decision", {"decision": "CONFIRMED_SUSPICIOUS", "reason": "pass-through of pooled inbound transfers with no business rationale"},
               token=ctx["analyst"])
        wb = h.get(f"/api/cases/{cid}/workbench", token=ctx["analyst"])
        assert wb["case"]["status"] == "CLOSED" and wb["case"]["decision"] == "CONFIRMED_SUSPICIOUS", wb["case"]
        stamps = [datetime.fromisoformat(e["ts"]) for e in wb["timeline"]]
        assert stamps == sorted(stamps) and all(s.tzinfo for s in stamps)
        events = {e["event"] for e in wb["timeline"]}
        assert {"priority_changed", "note"} <= events, events
        al = h.get(f"/api/monitoring/alerts/{ctx['mule_alert']['alert_id']}", token=ctx["analyst"])["alert"]
        assert al["status"] == "RESOLVED" and al["resolution"] == "CONFIRMED_SUSPICIOUS"
        h.call("POST", f"/api/cases/{cid}/transition", {"status": "INVESTIGATING"}, token=ctx["analyst"], expect=409)
        return {"timeline_events": len(wb["timeline"]), "sources": sorted({e["source"] for e in wb["timeline"]})}

    @step("Audit trail contains the journey and cannot be edited through the API")
    def s_audit():
        rows = h.get("/api/audit?limit=2000", token=ctx["admin"])
        actions = {r["action"] for r in rows}
        need = {"login", "monitoring_run", "transactions_ingested", "alert_assigned", "case_created", "case_priority_changed", "mule_assessment"}
        assert need <= actions, sorted(need - actions)
        assert {"decision_recorded", "case_closed"} & actions, actions
        for m in ("PUT", "PATCH", "DELETE"):
            h.call(m, "/api/audit", token=ctx["admin"], expect=(404, 405))
        mine = h.get("/api/audit?limit=50", token=ctx["analyst"])
        assert {r["user_id"] for r in mine} <= {"U-analyst"}  # analysts see only their own trail
        return {"audit_rows": len(rows), "actions_seen": sorted(actions)[:30]}

    @step("Operations: alert-quality metrics use valid denominators only; business metrics are exposed")
    def s_ops():
        q = h.get("/api/monitoring/quality", token=ctx["analyst"])
        o = q["overall"]
        assert o["decided"] >= 2 and o["confirmed"] >= 1 and o["false_positive"] >= 1, o
        assert abs(o["confirmation_rate"] + o["false_discovery_rate"] - 1.0) < 1e-9
        assert "false_positive_rate" in q["not_computed"] and "recall" in q["not_computed"]
        assert "false_positive_rate" not in json.dumps(q["overall"]) and "recall" not in json.dumps(q["overall"]).lower()
        k = h.get("/api/monitoring/kpis", token=ctx["analyst"])
        assert "false_positive_rate" not in k and "false_discovery_rate" in k
        m = h.call("GET", "/metrics", token=ctx["admin"])[1]
        for name in ("fira_alerts_open", "fira_cases_open", "fira_ingest_rows", "fira_data_quality_score", "fira_db_ping_ms", "fira_http_requests_total"):
            assert name in m, name
        return {"decided": o["decided"], "confirmation_rate": o["confirmation_rate"], "false_discovery_rate": o["false_discovery_rate"],
                "low_sample_flag": o["low_sample"], "open_alerts": k["open_alerts"]}

    @step("Configuration governance: a detector switch and a threshold change are logged with who, why, old and new")
    def s_config():
        h.post("/api/config/detectors/GEO_NEW_COUNTRY", {"enabled": False, "reason": "acceptance test: silence a noisy detector"}, token=ctx["admin"])
        h.post("/api/config/monitoring", {"path": "triage.thresholds.high", "value": 52, "reason": "acceptance test: calibration review"}, token=ctx["admin"])
        h.call("POST", "/api/config/monitoring", {"path": "triage.thresholds.high", "value": 70, "reason": "invalid ordering"}, token=ctx["admin"], expect=422)
        h.call("POST", "/api/config/monitoring", {"path": "triage.thresholds.high", "value": 53, "reason": "analyst must not"}, token=ctx["analyst"], expect=403)
        log = h.get("/api/config/changes?limit=20", token=ctx["analyst"])
        paths = {(r["path"], r["changed_by"]) for r in log}
        assert ("alerting.disabled_detectors", "U-admin") in paths and ("triage.thresholds.high", "U-admin") in paths, paths
        row = next(r for r in log if r["path"] == "triage.thresholds.high")
        assert (row["old_value"], row["new_value"]) == (50.0, 52.0) and row["reason"] and row["ts"]
        h.post("/api/config/detectors/GEO_NEW_COUNTRY", {"enabled": True, "reason": "acceptance test: restore"}, token=ctx["admin"])
        h.post("/api/config/monitoring", {"path": "triage.thresholds.high", "value": 50, "reason": "acceptance test: restore"}, token=ctx["admin"])
        return {"changes_recorded": len(log), "example": {k: row[k] for k in ("path", "old_value", "new_value", "changed_by", "source")}}

    @step("Security behaviour over HTTP: 401, 403, oversize 413, login lockout 429, logout revokes the token")
    def s_security():
        h.call("GET", "/api/monitoring/alerts", token=None, expect=401)
        h.call("POST", "/api/monitoring/run", {}, token=ctx["analyst"], expect=403)
        h.call("GET", "/metrics", token=ctx["analyst"], expect=403)
        big = json.dumps({"transactions": [{"x": "y" * 1000}] * 6000}).encode()
        try:
            h.call("POST", "/api/monitoring/transactions", token=ctx["admin"], raw=big, expect=413)
            oversize = "413"
        except (ConnectionError, urllib.error.URLError):  # the server may close the socket while the client is still sending
            oversize = "connection closed by server"
        user = f"nobody{uuid.uuid4().hex[:6]}"
        codes = [h.call("POST", "/api/auth/login", {"username": user, "password": "wrong-password-123"}, token=None, expect=(401, 429))[0] for _ in range(7)]
        assert codes[:5] == [401] * 5 and 429 in codes[5:], codes
        tok = h.login("analyst", a.analyst_password)
        h.call("GET", "/api/auth/me", token=tok)
        h.call("POST", "/api/auth/logout", token=tok)
        h.call("GET", "/api/auth/me", token=tok, expect=401)
        _, _, hd = h.call("GET", "/health", token=None)
        hl = {k.lower(): v for k, v in hd.items()}
        assert hl.get("x-content-type-options") == "nosniff" and hl.get("x-frame-options") == "DENY" and "x-request-id" in hl
        return {"login_sequence": codes, "oversize_request": oversize}

    if a.db_url:
        script = ROOT / "infrastructure" / "scripts" / "pg_backup.py"
        outdir = a.out.parent / "backups"
        env = dict(os.environ, DATABASE_URL=a.db_url, BACKUP_PASSPHRASE="acceptance-test-passphrase")  # noqa: S106 (throwaway, test only)
        docker = ["--docker-container", a.docker_container] if a.docker_container else []

        def run_py(*args: str) -> dict[str, Any]:
            p = subprocess.run([sys.executable, str(script), *args], env=env, capture_output=True, text=True, check=False)  # noqa: S603
            if p.returncode != 0:
                raise AssertionError(f"pg_backup {' '.join(args[:2])} exit {p.returncode}: {(p.stdout + p.stderr)[-400:]}")
            return json.loads(p.stdout)

        @step("Backup (encrypted), verify, and restore test into a scratch database")
        def s_backup():
            b = run_py("backup", "--out-dir", str(outdir), "--passphrase-env", "BACKUP_PASSPHRASE", *docker)
            ctx["archive"] = b["backup"]
            v = run_py("verify", b["backup"], "--passphrase-env", "BACKUP_PASSPHRASE", *docker)
            assert v["checksum_matches_manifest"] and v["pg_restore_can_read"] and v["encrypted"]
            t = run_py("restore-test", b["backup"], "--passphrase-env", "BACKUP_PASSPHRASE", *docker)
            assert t["passed"] and t["row_counts_match"] and t["alembic_revision_matches"] and all(t["append_only_triggers_present"].values()), t
            ctx["live_counts"] = b["row_counts"]
            return {"size_bytes": b["size_bytes"], "dump_seconds": b["dump_seconds"], "restore_seconds": t["restore_seconds"],
                    "row_counts_match": t["row_counts_match"], "tables_checked": len(t["row_count_detail"])}

        @step("Restore into a named database and verify the restored application")
        def s_restore():
            target = f"fira_accept_restored_{uuid.uuid4().hex[:6]}"
            ctx["restored_db"] = target
            run_py("restore", ctx["archive"], "--passphrase-env", "BACKUP_PASSPHRASE", "--target-db", target, *docker)
            from sqlalchemy.engine import make_url

            url = make_url(a.db_url.replace("postgresql://", "postgresql+psycopg://", 1) if a.db_url.startswith("postgresql://") else a.db_url)
            c = ctx["live_counts"]
            cmd = [sys.executable, str(ROOT / "infrastructure" / "scripts" / "verify_restored_app.py"), "--url",
                   url.set(database=target).render_as_string(hide_password=False), "--expect-alerts", str(c["monitoring_alerts"]),
                   "--expect-cases", str(c["cases"]), "--expect-batches", str(c["ingestion_batches"])]
            p = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)  # noqa: S603
            marker = '{\n  "passed"'
            out = p.stdout[p.stdout.rindex(marker):] if marker in p.stdout else p.stdout
            assert p.returncode == 0, out[-500:] + p.stderr[-300:]
            d = json.loads(out)
            assert d["passed"] and d["audit_append_only"] and d["database_accepts_writes"]
            return {"restored_database": target, "alerts": d["alerts"], "cases": d["cases"], "batches": d["ingestion_batches"],
                    "audit_append_only": d["audit_append_only"]}
    else:
        s_backup = s_restore = None  # type: ignore[assignment]

    for fn in (s_ready, s_dirty, s_run, s_triage, s_mule, s_fp, s_assign, s_case, s_decide, s_audit, s_ops, s_config, s_security, s_backup, s_restore):
        if fn is not None:
            fn()
    passed = sum(1 for r in RESULTS if r["passed"])
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "api": a.url, "steps": RESULTS, "passed": passed, "total": len(RESULTS)}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.with_suffix(".json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    lines = [f"# Acceptance run ({report['created_at'][:19]}Z)", "", f"{passed} of {len(RESULTS)} steps passed against `{a.url}`.", "",
             "| # | step | result | seconds |", "|---|---|---|---|"]
    for r in RESULTS:
        lines.append(f"| {r['n']} | {r['step']} | {'PASS' if r['passed'] else '**FAIL**'} | {r['seconds']} |")
    lines += ["", "## Evidence", ""]
    for r in RESULTS:
        lines += [f"### {r['n']}. {r['step']}", "", "```json", json.dumps(r["evidence"] if r["passed"] else {"error": r.get("error")}, indent=1, default=str), "```", ""]
    a.out.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n{passed}/{len(RESULTS)} steps passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
