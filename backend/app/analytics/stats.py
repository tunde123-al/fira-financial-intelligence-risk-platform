"""Behavioural statistics over a customer's transactions.

`split_customer_frame` orients a raw transaction frame relative to the customer's
own accounts (outbound / inbound / internal). `period_stats` summarises one period
and is used both for the baseline and for the investigation window, so the two
are always computed identically.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class OrientedFrames:
    outbound: pd.DataFrame
    inbound: pd.DataFrame
    internal: pd.DataFrame


def split_customer_frame(tx: pd.DataFrame, account_ids: set[str]) -> OrientedFrames:
    sender_own = tx.sender_account_id.isin(account_ids)
    receiver_own = tx.receiver_account_id.isin(account_ids)
    internal = tx[sender_own & receiver_own]
    outbound = tx[sender_own & ~receiver_own]
    inbound = tx[receiver_own & ~sender_own]
    return OrientedFrames(outbound=outbound, inbound=inbound, internal=internal)


def epoch_seconds(ts: pd.Series) -> np.ndarray:
    """UTC epoch seconds for a (tz-aware or naive-UTC) timestamp series."""
    if ts.empty:
        return np.zeros(0, dtype=np.int64)
    t = pd.to_datetime(ts, utc=True).dt.tz_localize(None)
    return t.to_numpy().astype("datetime64[s]").astype(np.int64)


def max_count_in_window(timestamps: pd.Series, minutes: int) -> tuple[int, pd.Timestamp | None]:
    """Largest number of events inside any rolling window of `minutes`."""
    if timestamps.empty:
        return 0, None
    t = np.sort(epoch_seconds(timestamps))
    width = minutes * 60
    right = np.searchsorted(t, t + width, side="right")
    counts = right - np.arange(len(t))
    i = int(np.argmax(counts))
    return int(counts[i]), pd.Timestamp(int(t[i]), unit="s", tz="UTC")


def daily_counts(timestamps: pd.Series, start: datetime, end: datetime) -> np.ndarray:
    days = pd.date_range(pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize(), freq="D")
    if len(days) == 0:
        return np.zeros(0)
    c = timestamps.dt.normalize().value_counts()
    return np.array([int(c.get(d, 0)) for d in days], dtype=float)


def robust_amount_stats(amounts: pd.Series) -> dict[str, float]:
    a = amounts.dropna().astype(float)
    if a.empty:
        return {"n": 0}
    med = float(a.median())
    mad = float((a - med).abs().median())
    return {
        "n": int(len(a)), "median": round(med, 2), "mad_sigma": round(mad * 1.4826, 2),
        "p95": round(float(a.quantile(0.95)), 2), "p99": round(float(a.quantile(0.99)), 2),
        "max": round(float(a.max()), 2), "mean": round(float(a.mean()), 2), "total": round(float(a.sum()), 2),
    }


def distribution(df: pd.DataFrame, cols: list[str]) -> dict[str, float]:
    if df.empty:
        return {}
    key = df[cols].astype(str).agg("|".join, axis=1)
    vc = key.value_counts(normalize=True)
    return {str(k): float(v) for k, v in vc.items()}


def jensen_shannon(p: dict[str, float], q: dict[str, float]) -> float:
    keys = sorted(set(p) | set(q))
    if not keys:
        return 0.0
    pa = np.array([p.get(k, 0.0) for k in keys]) + 1e-12
    qa = np.array([q.get(k, 0.0) for k in keys]) + 1e-12
    pa, qa = pa / pa.sum(), qa / qa.sum()
    m = 0.5 * (pa + qa)
    js = 0.5 * np.sum(pa * np.log2(pa / m)) + 0.5 * np.sum(qa * np.log2(qa / m))
    return float(max(0.0, min(1.0, js)))


def pass_through(inbound: pd.DataFrame, outbound: pd.DataFrame, hours: float = 24.0) -> dict[str, Any]:
    """Greedy FIFO matching of outbound value to preceding inbound credits within `hours`.

    Returns the share of inbound value that left again within the horizon, plus the
    matched (inbound, outbound) transaction id pairs used as evidence.
    """
    inc = inbound[inbound.status == "completed"].sort_values("timestamp")
    out = outbound[outbound.status == "completed"].sort_values("timestamp")
    total_in = float(inc.amount_usd.sum())
    if inc.empty or out.empty or total_in <= 0:
        return {"ratio": 0.0, "inbound_usd": round(total_in, 2), "matched_usd": 0.0, "n_inbound": int(len(inc)),
                "pairs": []}
    remaining = inc.amount_usd.to_numpy(dtype=float).copy()
    in_ts = epoch_seconds(inc.timestamp)
    in_ids = inc.transaction_id.to_numpy()
    horizon = int(hours * 3600)
    matched = 0.0
    pairs: list[tuple[str, str]] = []
    for o_ts, o_amt, o_id in zip(epoch_seconds(out.timestamp), out.amount_usd.to_numpy(dtype=float),
                                 out.transaction_id.to_numpy()):
        need = o_amt
        eligible = np.where((in_ts <= o_ts) & (in_ts >= o_ts - horizon) & (remaining > 0))[0]
        for i in eligible:
            if need <= 0:
                break
            take = min(need, remaining[i])
            remaining[i] -= take
            need -= take
            matched += take
            if len(pairs) < 50:
                pairs.append((str(in_ids[i]), str(o_id)))
    return {"ratio": round(matched / total_in, 4), "inbound_usd": round(total_in, 2),
            "matched_usd": round(matched, 2), "n_inbound": int(len(inc)), "pairs": pairs}


def inbound_counterparties(inbound: pd.DataFrame) -> pd.Series:
    """Counterparty key for each inbound credit: sender account, else external id."""
    return inbound.sender_account_id.fillna(inbound.external_counterparty).fillna("UNKNOWN")


def outbound_counterparties(outbound: pd.DataFrame, types: list[str] | None = None) -> pd.Series:
    """Beneficiary key of each completed outbound transfer: receiver account, else external id."""
    o = outbound[outbound.status == "completed"]
    if types is not None:
        o = o[o.transaction_type.isin(types)]
    return o.receiver_account_id.fillna(o.external_counterparty).dropna()


def band_window_stats(df: pd.DataFrame, amounts: pd.Series, hours: float) -> tuple[int, float, pd.Timestamp | None]:
    """Largest number of events inside any rolling `hours` window, with their summed amount.

    `df` and `amounts` are aligned. Returns (count, sum, window start); (0, 0.0, None) if empty.
    """
    if df.empty:
        return 0, 0.0, None
    order = np.argsort(epoch_seconds(df.timestamp), kind="stable")
    t = epoch_seconds(df.timestamp)[order]
    a = amounts.to_numpy(dtype=float)[order]
    csum = np.concatenate([[0.0], np.cumsum(a)])
    right = np.searchsorted(t, t + int(hours * 3600), side="right")
    counts = right - np.arange(len(t))
    sums = csum[right] - csum[np.arange(len(t))]
    # prefer more events, then more value
    i = int(np.lexsort((sums, counts))[-1])
    return int(counts[i]), float(sums[i]), pd.Timestamp(int(t[i]), unit="s", tz="UTC")


def period_stats(frames: OrientedFrames, start: datetime, end: datetime) -> dict[str, Any]:
    out = frames.outbound
    inn = frames.inbound
    done_out = out[out.status == "completed"]
    days = max((pd.Timestamp(end) - pd.Timestamp(start)).total_seconds() / 86400, 1.0)
    dc = daily_counts(out.timestamp, start, end)
    burst, burst_at = max_count_in_window(out.timestamp, 60)
    return {
        "period_start": pd.Timestamp(start).isoformat(), "period_end": pd.Timestamp(end).isoformat(),
        "days": round(days, 1),
        "n_outbound": int(len(out)), "n_inbound": int(len(inn)), "n_internal": int(len(frames.internal)),
        "n_failed_outbound": int((out.status != "completed").sum()),
        "outbound_usd": robust_amount_stats(done_out.amount_usd),
        "inbound_usd": robust_amount_stats(inn[inn.status == "completed"].amount_usd),
        "daily_outbound_mean": round(float(dc.mean()) if len(dc) else 0.0, 3),
        "daily_outbound_std": round(float(dc.std()) if len(dc) else 0.0, 3),
        "daily_outbound_max": int(dc.max()) if len(dc) else 0,
        "max_outbound_60min": burst, "max_outbound_60min_at": burst_at.isoformat() if burst_at is not None else None,
        "countries": sorted(set(out.country.dropna().astype(str)) | set(inn.country.dropna().astype(str))),
        "devices": sorted(set(out.device_id.dropna().astype(str))),
        "channels": distribution(out, ["channel"]),
        "type_channel_mix": distribution(out, ["transaction_type", "channel"]),
        "distinct_inbound_counterparties": int(inbound_counterparties(inn).nunique()),
        "distinct_inbound_per_30d": round(inbound_counterparties(inn).nunique() * 30.0 / days, 2),
        "distinct_outbound_per_30d": round(outbound_counterparties(out, ["transfer"]).nunique() * 30.0 / days, 2),
    }
