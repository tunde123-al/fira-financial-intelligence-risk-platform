"""Scripted end-to-end walkthrough of the monitoring workflow against a RUNNING FIRA API.

Standard library only. It performs the same steps an investigator would take in the UI:

  1. sign in as admin and run monitoring for one customer (alerts are created automatically)
  2. ingest a small batch of NEW near-threshold transactions and watch a STRUCTURING alert appear
  3. sign in as an analyst, open the alert, create a case, run the evidence-grounded investigation
  4. add a note, attach evidence, record a decision
  5. print the audit trail entries and the dashboard KPIs

Usage (the API must already be running with synthetic data loaded and both users bootstrapped):

    python infrastructure/scripts/demo_api_walkthrough.py --url http://localhost:8000 \
        --admin-password "$BOOTSTRAP_ADMIN_PASSWORD" --analyst-password "$BOOTSTRAP_ANALYST_PASSWORD"

Everything it creates is synthetic and recorded in the audit log like any other action.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.token: str | None = None

    def call(self, method: str, path: str, body: dict | None = None) -> dict | list:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=300) as r:  # nosec B310
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise SystemExit(f"{method} {path} -> HTTP {e.code}: {e.read().decode()[:300]}") from None

    def login(self, user: str, password: str) -> None:
        self.token = self.call("POST", "/api/auth/login", {"username": user, "password": password})["access_token"]


def step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--admin-password", required=True)
    ap.add_argument("--analyst-password", required=True)
    ap.add_argument("--ingest-only", action="store_true",
                    help="stop after step 2 (use the UI for the investigator steps)")
    ap.add_argument("--customer", default=None, help="customer to use for the structuring batch (default: first search hit for CUST-1)")
    a = ap.parse_args()
    admin, analyst = Api(a.url), Api(a.url)
    admin.login("admin", a.admin_password)
    analyst.login("analyst", a.analyst_password)

    customer = a.customer or admin.call("GET", "/api/search?q=CUST-10&entity_type=customer&limit=5")[0]["entity_id"]
    acc = admin.call("GET", f"/api/customers/{customer}")["accounts"][0]
    as_of = datetime.fromisoformat(admin.call("GET", "/api/dashboard")["as_of"].replace("Z", "+00:00"))

    step(1, f"Admin runs monitoring for {customer}")
    run = admin.call("POST", "/api/monitoring/run", {"customer_ids": [customer]})
    print(f"    run {run['run_id']}: {run['alerts_created']} created, {run['alerts_updated']} merged, {run['duration_ms']} ms")

    step(2, "Admin ingests 4 new near-threshold transactions (USD 9,100 to 9,600, within 7 hours)")
    # the synthetic bank's fixed illustrative FX table (units of currency per USD)
    fx = {"NGN": 1500.0, "GHS": 15.0, "KES": 130.0, "ZAR": 18.0, "GBP": 0.78, "USD": 1.0, "CAD": 1.36, "AED": 3.67,
          "CNY": 7.2, "TRY": 34.0}
    rows = []
    for i, usd in enumerate([9100, 9300, 9450, 9600]):
        rows.append({"timestamp": (as_of - timedelta(hours=7 - 2 * i)).astimezone(timezone.utc).isoformat(),
                     "sender_account_id": acc["account_id"], "currency": acc["currency"], "amount_usd": usd,
                     "amount": round(usd * fx[acc["currency"]], 2), "transaction_type": "transfer", "channel": "web",
                     "external_counterparty": "EXT-DEMO-STRUCT"})
    out = admin.call("POST", "/api/monitoring/transactions", {"transactions": rows, "run_monitoring": True})
    print(f"    accepted {out['accepted']}, rejected {out['rejected_count']}; monitoring created "
          f"{out.get('monitoring_run', {}).get('alerts_created')} alert(s)")

    if a.ingest_only:
        print("\n    Done. Now sign in as the analyst in the UI and open the Alert Queue.")
        return 0

    step(3, "Analyst opens the alert queue")
    alerts = analyst.call("GET", f"/api/monitoring/alerts?customer_id={customer}&status=NEW&limit=20")["items"]
    if not alerts:
        print("    no NEW alerts for this customer; pick another with --customer")
        return 1
    top = max(alerts, key=lambda x: x["risk_score"])
    for al in alerts:
        print(f"    {al['alert_id']} {al['severity']:8} score {al['risk_score']:5.1f}  {al['detector_id']}: {al['description'][:90]}")
    detail = analyst.call("GET", f"/api/monitoring/alerts/{top['alert_id']}")
    print(f"    detail: {len(detail['transactions'])} supporting transactions, events {[e['event_type'] for e in detail['events']]}")

    step(4, "Analyst creates a case, runs the evidence-grounded investigation, adds a note and evidence")
    case = analyst.call("POST", f"/api/monitoring/alerts/{top['alert_id']}/case", {})["case"]
    cid = case["case_id"]
    print(f"    case {case['case_number']} ({case['status']}, priority {case['priority']})")
    analyst.call("POST", f"/api/cases/{cid}/transition", {"status": "INVESTIGATING"})
    inv = analyst.call("POST", f"/api/cases/{cid}/investigate", {})
    print(f"    investigation {inv['investigation_id']} status {inv['status']}")
    analyst.call("POST", f"/api/cases/{cid}/notes", {"body": "Reviewed the near-threshold pattern; checking customer's stated activity."})
    if top["transaction_ids"]:
        analyst.call("POST", f"/api/cases/{cid}/evidence", {"kind": "transaction", "ref": top["transaction_ids"][0]})
    wb = analyst.call("GET", f"/api/cases/{cid}/workbench")
    print(f"    workbench: risk {wb['risk']['score']} ({wb['risk']['band']}); components "
          f"{[(x['label'], x['points']) for x in wb['risk']['score_components'][:3]]}")

    step(5, "Analyst records the decision")
    res = analyst.call("POST", f"/api/cases/{cid}/decision",
                       {"decision": "CONFIRMED_SUSPICIOUS", "reason": "Repeated near-threshold transfers with no business explanation (synthetic demo)."})
    print(f"    case {res['case']['status']} / {res['case']['decision']}; alerts updated {res['alerts_updated']}; investigation {res['investigation']}")

    step(6, "Audit trail and KPIs")
    for e in admin.call("GET", "/api/audit?limit=2000"):
        if e["entity_id"] in {cid, top["alert_id"]}:
            print(f"    {e['ts'][:19]} {e['user_id']:10} {e['action']:26} {json.dumps(e['details'])[:70]}")
    k = admin.call("GET", "/api/monitoring/kpis")
    print(f"    KPIs: alerts {k['alerts_generated']}, open {k['open_alerts']}, cases by status {k['cases_by_status']}, "
          f"false-discovery rate {k['false_discovery_rate']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
