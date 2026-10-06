"""Money-mule risk indicators for a customer: an evidence-backed consolidated view, never a verdict.

Money mules receive illicit funds and pass them on, often quickly and through accounts that look unremarkable on their
own. No single behaviour proves it, and several benign customers share individual behaviours (a marketplace seller
has fan-in, a payroll business has fan-out). So this module reports *indicators* with the transactions that support each
one, adds them into an explainable 0-100 score, and states plainly that the result is a prompt for a human review.

Eight indicators, maxima summing to 100 (thresholds in `monitoring_config.yaml`, section `mule:`):
fan-in 20, fan-out 15, rapid movement 20, low retention 10, new or dormant account 10, shared device / identity 10,
flagged network 10, layering chain or cycle 5. Fan-in plus fan-out alone can reach at most 35 (MEDIUM): high scores need
the fund-flow behaviour together with account and network context.

Inputs are only transactions, account attributes, the existing risk-engine signals and the transfer graph. Ground-truth
labels are never read.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from statistics import median
from typing import Any

import pandas as pd

DISCLAIMER = ("Money-mule risk indicators, not proof. Each indicator can occur in legitimate activity; "
              "a human investigator decides.")
MAX_POINTS = {"fan_in": 20.0, "fan_out": 15.0, "rapid_movement": 20.0, "low_retention": 10.0,
              "new_or_dormant_account": 10.0, "shared_device_identity": 10.0, "flagged_network": 10.0,
              "layering_chain": 5.0}
LABELS = {"fan_in": "Fan-in: many distinct senders", "fan_out": "Fan-out: many distinct beneficiaries",
          "rapid_movement": "Rapid movement: inbound value leaves within hours",
          "low_retention": "Low retention: little of the inbound value remains",
          "new_or_dormant_account": "New or dormant account", "shared_device_identity": "Shared device or identifier",
          "flagged_network": "Connected to customers with open alerts",
          "layering_chain": "Onward chain or circular path"}


def _utc(ts: Any) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def band_for(score: float, bands: dict[str, float]) -> str:
    if score >= bands["high"]:
        return "HIGH"
    if score >= bands["medium"]:
        return "MEDIUM"
    if score >= bands["low"]:
        return "LOW"
    return "NONE"


def fifo_dwell(inbound: pd.DataFrame, outbound: pd.DataFrame, hours: float) -> tuple[float, list[float], list[str]]:
    """Match outbound value to earlier inbound value first-in-first-out within `hours`.

    Returns (matched USD, dwell hours of each matched slice, outbound transaction ids that consumed inbound value).
    """
    window = pd.Timedelta(hours=hours)
    queue: deque[list[Any]] = deque([[r.timestamp, float(r.amount_usd)] for r in
                                     inbound.sort_values("timestamp").itertuples()])
    matched, dwell, ids = 0.0, [], []
    for r in outbound.sort_values("timestamp").itertuples():
        left = float(r.amount_usd)
        while left > 1e-9 and queue:
            t_in, rem = queue[0]
            if t_in > r.timestamp:
                break
            if r.timestamp - t_in > window:
                queue.popleft()
                continue
            take = min(left, rem)
            matched += take
            left -= take
            dwell.append((r.timestamp - t_in).total_seconds() / 3600.0)
            if r.transaction_id not in ids:
                ids.append(r.transaction_id)
            if take >= rem - 1e-9:
                queue.popleft()
            else:
                queue[0][1] = rem - take
    return matched, dwell, ids


def assess_mule(store: Any, graph: Any, assessment: Any, customer_id: str, end: datetime, cfg: Any,
                flagged_of: Callable[[list[str]], set[str]] | None = None) -> dict[str, Any]:
    """Compute the mule indicators for one customer. `assessment` is the risk-engine assessment (may be None);
    `flagged_of` returns which of the given customers currently carry an open alert."""
    start = end - timedelta(days=cfg.window_days)
    accounts = store.accounts_for_customer(customer_id)
    own = {a.account_id for a in accounts}
    tx = store.transactions_for_accounts(sorted(own), start, end)
    tx = tx[tx.status == "completed"] if not tx.empty else tx
    if not tx.empty:
        tx = tx.assign(timestamp=pd.to_datetime(tx["timestamp"], utc=True))
    has_rows = not tx.empty
    # inbound = transfers from other accounts (salary and cash deposits have no sending account and are legitimate income)
    inbound = (tx[tx.receiver_account_id.isin(own) & tx.sender_account_id.notna() & ~tx.sender_account_id.isin(own)]
               if has_rows else tx)
    outbound = tx[tx.sender_account_id.isin(own) & ~tx.receiver_account_id.isin(own)] if has_rows else tx
    in_usd = float(inbound.amount_usd.sum()) if len(inbound) else 0.0
    out_usd = float(outbound.amount_usd.sum()) if len(outbound) else 0.0
    points = {c.signal_type: c.points for c in (assessment.contributors if assessment else [])}
    out: list[dict[str, Any]] = []

    def add(iid: str, pts: float, observed: Any, threshold: Any, reason: str, evidence: dict[str, Any] | None = None) -> None:
        mx = MAX_POINTS[iid]
        pts = round(max(0.0, min(pts, mx)), 2)
        out.append({"id": iid, "label": LABELS[iid], "fired": pts > 0, "points": pts, "max": mx, "observed": observed,
                    "threshold": threshold, "reason": reason, "evidence": evidence or {}})

    # 1 fan-in
    senders = sorted({s for s in inbound.sender_account_id.dropna()}) if len(inbound) else []
    n_in = len(senders)
    add("fan_in", MAX_POINTS["fan_in"] * min(n_in / (2 * cfg.fan_in_min), 1.0) if n_in >= cfg.fan_in_min else 0.0, n_in,
        cfg.fan_in_min, f"{n_in} distinct sending accounts in {cfg.window_days} days (indicator from {cfg.fan_in_min})",
        {"accounts": senders[:20], "transaction_ids": inbound.transaction_id.head(10).tolist() if len(inbound) else []})
    # 2 fan-out
    recv = sorted({r for r in outbound.receiver_account_id.dropna()}) if len(outbound) else []
    ext = sorted({e for e in outbound.external_counterparty.dropna()}) if len(outbound) and "external_counterparty" in outbound else []
    n_out = len(recv) + len(ext)
    add("fan_out", MAX_POINTS["fan_out"] * min(n_out / (2 * cfg.fan_out_min), 1.0) if n_out >= cfg.fan_out_min else 0.0,
        n_out, cfg.fan_out_min, f"{n_out} distinct beneficiaries ({len(recv)} accounts, {len(ext)} external) "
        f"in {cfg.window_days} days (indicator from {cfg.fan_out_min})",
        {"accounts": recv[:20], "external": ext[:10],
         "transaction_ids": outbound.transaction_id.head(10).tolist() if len(outbound) else []})
    # 3 rapid movement (FIFO matching of outbound to earlier inbound value)
    matched, dwell, moved_ids = fifo_dwell(inbound, outbound, cfg.rapid_hours) if in_usd > 0 and out_usd > 0 else (0.0, [], [])
    share = matched / in_usd if in_usd else 0.0
    fire = in_usd >= cfg.min_usd and share >= cfg.rapid_share
    scaled = (share - cfg.rapid_share) / max(1.0 - cfg.rapid_share, 1e-9)
    add("rapid_movement", MAX_POINTS["rapid_movement"] * (0.5 + 0.5 * min(max(scaled, 0.0), 1.0)) if fire else 0.0,
        round(share, 3), cfg.rapid_share,
        (f"{share:.0%} of USD {in_usd:,.0f} inbound left within {cfg.rapid_hours:g} h "
         f"(median dwell {median(dwell):.1f} h)") if dwell else "no inbound value matched to outbound value in the window",
        {"inbound_usd": round(in_usd, 2), "matched_usd": round(matched, 2),
         "median_dwell_hours": round(median(dwell), 2) if dwell else None, "transaction_ids": moved_ids[:10]})
    # 4 low retention
    first_in = inbound.timestamp.min() if len(inbound) else None
    # only outflow during the collection period and the following 3 x rapid_hours counts: unrelated spending
    # elsewhere in the window says nothing about what happened to the inbound value
    if len(inbound):
        after = outbound[(outbound.timestamp >= first_in)
                         & (outbound.timestamp <= inbound.timestamp.max() + pd.Timedelta(hours=3 * cfg.rapid_hours))] \
            if len(outbound) else outbound
    else:
        after = outbound.iloc[0:0] if len(outbound) else outbound
    out_after = float(after.amount_usd.sum()) if len(after) else 0.0
    retained = (in_usd - out_after) / in_usd if in_usd else None
    fire = in_usd >= cfg.min_usd and out_after > 0 and retained is not None and retained <= cfg.retention_max
    add("low_retention", MAX_POINTS["low_retention"] if fire else 0.0,
        round(retained, 3) if retained is not None else None, cfg.retention_max,
        (f"{max(retained, 0):.0%} of inbound value retained (USD {in_usd:,.0f} in, {out_after:,.0f} out during and "
         f"within {3 * cfg.rapid_hours:g} h after the collection)"
         if retained is not None else "no inbound value in the window"),
        {"inbound_usd": round(in_usd, 2), "outbound_usd": round(out_after, 2),
         "transaction_ids": after.transaction_id.head(10).tolist() if len(after) else []})
    # 5 new or dormant account
    young = [a.account_id for a in accounts if first_in is not None and a.opened_at is not None
             and (first_in - _utc(a.opened_at)).days < cfg.new_account_days]
    dormant = points.get("DORMANT_REACTIVATION", 0.0) > 0
    add("new_or_dormant_account", MAX_POINTS["new_or_dormant_account"] if (young and len(inbound)) or dormant else 0.0,
        {"new_accounts": young, "dormant_reactivation": dormant}, cfg.new_account_days,
        "account opened shortly before its first inbound transfer" if young
        else "long-inactive account became active" if dormant else "account age and activity history look established",
        {"accounts": young})
    # 6 shared device / identifier
    shared = graph.shared_devices(customer_id) if graph is not None and hasattr(graph, "shared_devices") else []
    ident = points.get("SHARED_IDENTIFIER", 0.0) > 0
    dev_pts = 6.0 if shared else 0.0
    add("shared_device_identity", dev_pts + (4.0 if ident else 0.0) if (shared or ident) else 0.0,
        {"shared_devices": len(shared), "shared_identifier": ident}, 1,
        f"device shared with {max((s.n_customers for s in shared), default=1) - 1} other customer(s)" if shared
        else "identifier shared with other customers" if ident else "no shared devices or identifiers",
        {"devices": [s.device_id for s in shared[:5]], "customers": sorted({c for s in shared for c in s.customers} - {customer_id})[:10]})
    # 7 flagged network
    flagged: list[str] = []
    if graph is not None and hasattr(graph, "customer_counterparties") and flagged_of is not None:
        try:
            owners = {r["owner_customer_id"] for r in graph.customer_counterparties(customer_id, 2, start, 100)
                      if r.get("owner_customer_id")} - {customer_id}
            flagged = sorted(flagged_of(sorted(owners))) if owners else []
        except Exception:  # an unknown customer in the graph projection is "no network evidence", not an error
            flagged = []
    add("flagged_network", MAX_POINTS["flagged_network"] * min(len(flagged) / 3.0, 1.0) if flagged else 0.0,
        len(flagged), 1, f"{len(flagged)} transfer counterpart{'y' if len(flagged) == 1 else 'ies'} "
        "(within 2 hops) have open alerts" if flagged else "no transfer counterparties (within 2 hops) with open alerts",
        {"customers": flagged[:10]})
    # 8 layering chain / circular flow
    chain_depth, chain_ex = 0, []
    if graph is not None and hasattr(graph, "trace_funds"):
        for a in sorted(own):
            for f in graph.trace_funds(a, "out", 3, start, 10):
                if f.hops > chain_depth and f.min_edge_usd >= cfg.min_usd * 0.5:
                    chain_depth, chain_ex = f.hops, f.path
    circular = points.get("CIRCULAR_FLOW", 0.0) > 0
    add("layering_chain", MAX_POINTS["layering_chain"] if chain_depth >= 3 or circular else 0.0,
        {"onward_hops": chain_depth, "circular": circular}, 3,
        "value continues through 3+ accounts" if chain_depth >= 3 else "funds return to origin" if circular
        else "no onward chain of 3 or more accounts", {"path": chain_ex})
    score = round(sum(i["points"] for i in out), 1)
    band = band_for(score, cfg.bands)
    fired = [i["label"] for i in out if i["fired"]]
    cust = store.get_customer(customer_id)
    context = []
    if cust is not None and cust.customer_type != "individual" and (n_in >= cfg.fan_in_min or n_out >= cfg.fan_out_min):
        context.append("Business account: many counterparties are common for legitimate businesses; read fan-in/fan-out "
                       "together with the other indicators.")
    if not has_rows:
        context.append("No completed transactions in the window: nothing to assess.")
    return {"customer_id": customer_id, "window": [start.isoformat(), end.isoformat()], "score": score, "band": band,
            "bands": cfg.bands, "indicators": out, "fired": fired, "inbound_usd": round(in_usd, 2),
            "outbound_usd": round(out_usd, 2), "context": context, "disclaimer": DISCLAIMER,
            "summary": (f"{band} money-mule risk indicators ({score}/100): " + "; ".join(fired)) if fired
            else "No money-mule risk indicators in the window."}
