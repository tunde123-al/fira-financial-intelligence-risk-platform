"""Dataset data-quality audit: what is wrong with the data FIRA actually holds, computed from the data.

This is the *stored dataset* counterpart of the ingestion gate (`app/monitoring/ingest.py`, which judges rows as they arrive).
Every figure is calculated from the tables passed in; nothing is hard-coded. One pandas implementation serves both stores
(the PostgreSQL store loads only the columns it needs, so results are identical in both modes).

A transaction is **valid** when it trips none of the transaction checks. `quality_score` = 100 x valid / processed. The other
tables are audited separately and do not change that score.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from app.monitoring.ingest import TYPE_DIRECTION
from app.synthetic.reference import FX_PER_USD

STATUSES = {"completed", "failed", "reversed", "pending"}
LIKELY_DUPLICATE_SECONDS = 60
FUTURE_TOLERANCE = timedelta(days=1)

TXN_CHECKS: dict[str, str] = {
    "missing_transaction_id": "transaction_id is empty",
    "duplicate_transaction_id": "the same transaction_id appears more than once (every repeat counted)",
    "likely_duplicate": f"same sender, receiver, amount and currency within {LIKELY_DUPLICATE_SECONDS} s of the previous such row",
    "invalid_amount": "amount missing, not finite, or not greater than zero",
    "invalid_currency": "currency is not one of the configured currencies",
    "invalid_timestamp": "timestamp missing or unparseable",
    "future_timestamp": "timestamp is more than one day after the dataset's as-of date",
    "no_account": "neither a sender nor a receiver account",
    "orphan_sender_account": "sender_account_id does not exist in accounts",
    "orphan_receiver_account": "receiver_account_id does not exist in accounts",
    "orphan_merchant": "merchant_id does not exist in merchants",
    "orphan_device": "device_id does not exist in devices",
    "impossible_state": "status outside the allowed set, or a type that needs a side the row lacks (e.g. deposit without a receiver)",
    "incomplete_record": "transaction_type or channel is missing",
}


def _blank(s: pd.Series) -> pd.Series:
    return s.isna() | (s.astype(str).str.strip() == "")


def audit(customers: pd.DataFrame, accounts: pd.DataFrame, tx: pd.DataFrame, merchants: pd.DataFrame, devices: pd.DataFrame,
          as_of: datetime, now: datetime | None = None) -> dict[str, Any]:
    t0 = time.perf_counter()
    now = now or datetime.now(timezone.utc)
    n = len(tx)
    flags: dict[str, pd.Series] = {}
    if n:
        tid = tx["transaction_id"]
        flags["missing_transaction_id"] = _blank(tid)
        flags["duplicate_transaction_id"] = tid.duplicated(keep="first") & ~flags["missing_transaction_id"]
        amount = pd.to_numeric(tx["amount"], errors="coerce")
        flags["invalid_amount"] = amount.isna() | ~np.isfinite(amount.fillna(np.nan)) | (amount <= 0)
        flags["invalid_currency"] = ~tx["currency"].astype(str).str.upper().isin(FX_PER_USD.keys())
        ts = pd.to_datetime(tx["timestamp"], utc=True, errors="coerce")
        flags["invalid_timestamp"] = ts.isna()
        limit = pd.Timestamp(as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)) + FUTURE_TOLERANCE
        flags["future_timestamp"] = ts > limit
        s_blank, r_blank = _blank(tx["sender_account_id"]), _blank(tx["receiver_account_id"])
        flags["no_account"] = s_blank & r_blank
        known = set(accounts["account_id"].astype(str))
        flags["orphan_sender_account"] = ~s_blank & ~tx["sender_account_id"].astype(str).isin(known)
        flags["orphan_receiver_account"] = ~r_blank & ~tx["receiver_account_id"].astype(str).isin(known)
        m_blank = _blank(tx["merchant_id"]) if "merchant_id" in tx else pd.Series(True, index=tx.index)
        d_blank = _blank(tx["device_id"]) if "device_id" in tx else pd.Series(True, index=tx.index)
        flags["orphan_merchant"] = ~m_blank & ~tx.get("merchant_id", pd.Series(index=tx.index, dtype=str)).astype(str).isin(set(merchants["merchant_id"].astype(str)))
        flags["orphan_device"] = ~d_blank & ~tx.get("device_id", pd.Series(index=tx.index, dtype=str)).astype(str).isin(set(devices["device_id"].astype(str)))
        ttype = tx["transaction_type"].astype(str).str.lower()
        need_s = ttype.map(lambda x: TYPE_DIRECTION.get(x) == "S")
        need_r = ttype.map(lambda x: TYPE_DIRECTION.get(x) == "R")
        bad_state = ~tx["status"].astype(str).str.lower().isin(STATUSES)
        flags["impossible_state"] = bad_state | (need_s & s_blank) | (need_r & r_blank)
        flags["incomplete_record"] = _blank(tx["transaction_type"]) | _blank(tx["channel"])
        # likely duplicates: same parties, amount and currency within a minute of the previous one
        parts = [tx[c].fillna("").astype(str) for c in ("sender_account_id", "receiver_account_id", "currency")]
        parts.append(amount.round(2).astype(str))
        k = parts[0] + "|" + parts[1] + "|" + parts[2] + "|" + parts[3]
        order = pd.DataFrame({"k": k, "ts": ts}).dropna()
        order = order.sort_values(["k", "ts"], kind="mergesort")
        gap = order.groupby("k")["ts"].diff()
        dup_idx = order.index[(gap <= pd.Timedelta(seconds=LIKELY_DUPLICATE_SECONDS)) & (gap >= pd.Timedelta(0))]
        likely = pd.Series(False, index=tx.index)
        likely.loc[dup_idx] = True
        flags["likely_duplicate"] = likely & ~flags["duplicate_transaction_id"]
    else:
        flags = {k: pd.Series(dtype=bool) for k in TXN_CHECKS}
    any_issue = pd.concat(list(flags.values()), axis=1).any(axis=1) if n else pd.Series(dtype=bool)
    with_issue = int(any_issue.sum())
    cust_known = set(customers["customer_id"].astype(str))
    acc_orphans = int((~accounts["customer_id"].astype(str).isin(cust_known)).sum()) if len(accounts) else 0
    acc_status = int(_blank(accounts["status"]).sum()) if len(accounts) else 0
    cust_incomplete = int((_blank(customers["country"]) | _blank(customers["segment"])).sum()) if len(customers) else 0
    cust_dup = int(customers["customer_id"].duplicated().sum()) if len(customers) else 0
    latest = pd.to_datetime(tx["timestamp"], utc=True, errors="coerce").max() if n else pd.NaT
    as_of_utc = pd.Timestamp(as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc))
    return {
        "records_processed": n, "valid": n - with_issue, "with_issues": with_issue,
        "quality_score": round(100.0 * (n - with_issue) / n, 2) if n else None,
        "checks": {k: {"count": int(flags[k].sum()), "description": TXN_CHECKS[k]} for k in TXN_CHECKS},
        "other_tables": {
            "customers": {"records": len(customers), "duplicate_ids": cust_dup, "incomplete_records": cust_incomplete},
            "accounts": {"records": len(accounts), "orphan_customer_references": acc_orphans, "missing_status": acc_status},
            "merchants": {"records": len(merchants)}, "devices": {"records": len(devices)}},
        "freshness": {"dataset_as_of": as_of_utc.isoformat(), "latest_transaction": latest.isoformat() if pd.notna(latest) else None,
                      "dataset_age_days": round((pd.Timestamp(now) - as_of_utc).total_seconds() / 86400.0, 1),
                      "note": "the dataset is a frozen synthetic snapshot, so its age is expected; a live feed would alert on this"},
        "definitions": {"valid": "a transaction that trips none of the checks", "quality_score": "100 x valid / records processed",
                        "scope": "stored transactions; the ingestion gate (see /api/data-quality/summary) judges rows as they arrive"},
        "computed_at": now.isoformat(), "duration_ms": int((time.perf_counter() - t0) * 1000),
    }
