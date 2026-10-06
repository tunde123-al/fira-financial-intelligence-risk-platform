"""Time-window activity analysis (1 hour, 24 hours, 7 days, 30 days).

Read-only descriptive statistics for the investigator: counts, value and distinct counterparties inside
each window ending at `end`, next to what the customer's own baseline would predict for a window of
that length. This is context, not a detector; thresholds that raise alerts live in the risk
configuration.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from app.analytics.stats import inbound_counterparties, outbound_counterparties, split_customer_frame

WINDOWS: dict[str, timedelta] = {
    "1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30),
}


def _summ(df: pd.DataFrame) -> dict[str, Any]:
    done = df[df.status == "completed"]
    return {"count": int(len(df)), "failed": int((df.status != "completed").sum()),
            "volume_usd": round(float(done.amount_usd.sum()), 2) if len(done) else 0.0}


def activity_windows(store: Any, customer_id: str, end: datetime | None = None, baseline_days: int = 90,
                     windows: list[str] | None = None) -> dict[str, Any]:
    end = end or store.as_of()
    names = [w for w in (windows or list(WINDOWS)) if w in WINDOWS]
    accounts = {a.account_id for a in store.accounts_for_customer(customer_id)}
    longest = max((WINDOWS[w] for w in names), default=timedelta(days=30))
    start = end - longest - timedelta(days=baseline_days)
    tx = store.transactions_for_accounts(sorted(accounts), start, end)
    out_rows: list[dict[str, Any]] = []
    for w in names:
        wstart = end - WINDOWS[w]
        win = tx[(tx.timestamp > pd.Timestamp(wstart)) & (tx.timestamp <= pd.Timestamp(end))]
        base = tx[(tx.timestamp <= pd.Timestamp(end - longest)) & (tx.timestamp > pd.Timestamp(start))]
        wf, bf = split_customer_frame(win, accounts), split_customer_frame(base, accounts)
        base_days = max((end - longest - start).total_seconds() / 86400, 1.0)
        scale = WINDOWS[w].total_seconds() / 86400 / base_days
        b_out = _summ(bf.outbound)
        row: dict[str, Any] = {
            "window": w, "start": wstart.isoformat(), "end": end.isoformat(),
            "outbound": _summ(wf.outbound), "inbound": _summ(wf.inbound),
            "distinct_beneficiaries": int(outbound_counterparties(wf.outbound, ["transfer"]).nunique()),
            "distinct_senders": int(inbound_counterparties(wf.inbound).nunique()) if len(wf.inbound) else 0,
            "baseline_expected": {"outbound_count": round(b_out["count"] * scale, 2),
                                  "outbound_volume_usd": round(b_out["volume_usd"] * scale, 2)},
        }
        exp = row["baseline_expected"]["outbound_count"]
        row["outbound_count_vs_baseline"] = round(row["outbound"]["count"] / exp, 2) if exp > 0 else None
        out_rows.append(row)
    return {"customer_id": customer_id, "as_of": end.isoformat(), "baseline_days": baseline_days, "windows": out_rows,
            "note": "Descriptive context only; alert thresholds are configured in the risk configuration."}
