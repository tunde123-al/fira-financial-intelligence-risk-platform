"""Validation and batch accounting for incoming transactions (the data-quality gate).

Every row of a batch ends in exactly one of two places: accepted (stored and monitored) or rejected (quarantined
with a reason code and a sanitised copy of the row). Accepted rows may additionally be flagged *late*. The module is
pure: it does I/O only through the lookup callables it is handed.

Reason codes and the groups used for the dashboard counts:

* malformed  : the row cannot be interpreted (not an object, unparseable values, bad id/IP/currency format)
* duplicate  : DUPLICATE_ID (same transaction id) or LIKELY_DUPLICATE (same parties/amount within a short window)
* invalid    : parsed but violates a business rule (amount, type/direction, range, closed account)
* referential: refers to an account, device or merchant that does not exist
"""
from __future__ import annotations

import ipaddress
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.data.store import TXN_FRAME_COLUMNS

REQUIRED = ["timestamp", "amount", "currency", "transaction_type", "channel"]
TXN_ID_RE = re.compile(r"^TXN-\d{1,12}$")
MAX_BATCH = 50_000

MALFORMED_CODES = {"MALFORMED_ROW", "MISSING_FIELD", "INVALID_TIMESTAMP", "INVALID_AMOUNT", "INVALID_CURRENCY",
                   "INVALID_ID_FORMAT", "INVALID_IP", "INVALID_STATUS"}
DUPLICATE_CODES = {"DUPLICATE_ID", "LIKELY_DUPLICATE"}
REFERENTIAL_CODES = {"UNKNOWN_ACCOUNT", "UNKNOWN_DEVICE", "UNKNOWN_MERCHANT"}
INVALID_CODES = {"NON_POSITIVE_AMOUNT", "UNSUPPORTED_CURRENCY", "TIMESTAMP_OUT_OF_RANGE", "INVALID_TYPE",
                 "INVALID_CHANNEL", "DIRECTION_MISMATCH", "NO_ACCOUNT", "ACCOUNT_CLOSED", "MISSING_TRANSACTION_ID"}
ALL_CODES = MALFORMED_CODES | DUPLICATE_CODES | REFERENTIAL_CODES | INVALID_CODES

# which side a transaction type needs: S = sender account, R = receiver account
TYPE_DIRECTION = {"deposit": "R", "salary": "R", "cash_withdrawal": "S", "card_payment": "S", "bill_payment": "S",
                  "transfer": "S"}


def code_group(code: str) -> str:
    if code in MALFORMED_CODES:
        return "malformed"
    if code in DUPLICATE_CODES:
        return "duplicate"
    if code in REFERENTIAL_CODES:
        return "referential"
    return "invalid"


@dataclass
class QualityRules:
    transaction_types: list[str] = field(default_factory=lambda: sorted(TYPE_DIRECTION))
    channels: list[str] = field(default_factory=lambda: ["web", "mobile", "pos", "atm", "branch", "api"])
    statuses: list[str] = field(default_factory=lambda: ["completed", "failed", "reversed", "pending"])
    max_age_days: int = 3650
    max_future_hours: int = 24
    late_after_hours: int = 24
    likely_duplicate_window_seconds: int = 60
    duplicate_policy: str = "reject"  # reject | accept
    reject_closed_accounts: bool = True


@dataclass
class Lookups:
    """Everything the validator needs from the store."""

    owners: Callable[[list[str]], dict[str, str]]
    account_status: Callable[[list[str]], dict[str, str]]
    known_devices: Callable[[list[str]], set[str]]
    known_merchants: Callable[[list[str]], set[str]]
    existing_ids: Callable[[list[str]], set[str]]
    recent_transactions: Callable[[list[str], Any, Any], pd.DataFrame]
    latest_timestamp: Callable[[], Any]
    reference_time: Callable[[], Any]


@dataclass
class BatchResult:
    accepted: pd.DataFrame
    rejected: list[dict[str, Any]] = field(default_factory=list)
    late_ids: list[str] = field(default_factory=list)
    ids_generated: int = 0
    received: int = 0
    reason_counts: dict[str, int] = field(default_factory=dict)

    def group_count(self, group: str) -> int:
        return sum(n for c, n in self.reason_counts.items() if code_group(c) == group)


def _fx(currency: str) -> float | None:
    from app.synthetic import reference as ref

    return ref.FX_PER_USD.get(currency)


def sanitise_row(row: Any) -> dict[str, Any]:
    """A bounded, storable copy of a rejected row. IP addresses are reduced to a /16 prefix like API masking does."""
    if not isinstance(row, dict):
        return {"_raw_type": type(row).__name__, "_raw": str(row)[:200]}
    out: dict[str, Any] = {}
    for k, v in list(row.items())[:30]:
        key = str(k)[:40]
        if key == "ip_address" and isinstance(v, str):
            parts = v.split(".")
            out[key] = ".".join([*parts[:2], "x", "x"]) if len(parts) == 4 else "masked"
        elif isinstance(v, (str, int, float, bool)) or v is None:
            out[key] = v[:120] if isinstance(v, str) else v
        else:
            out[key] = str(v)[:120]
    return out


def _is_blank(v: Any) -> bool:
    return v is None or (isinstance(v, float) and v != v) or (isinstance(v, str) and not v.strip())


def validate_batch(rows: list[Any], lk: Lookups, rules: QualityRules | None = None,
                   require_transaction_id: bool = False) -> BatchResult:
    """Validate a batch. Returns accepted rows (all `TXN_FRAME_COLUMNS`) and rejected rows with reason codes."""
    rules = rules or QualityRules()
    if len(rows) > MAX_BATCH:
        raise ValueError(f"batch too large: {len(rows)} > {MAX_BATCH} rows")
    res = BatchResult(accepted=pd.DataFrame(columns=TXN_FRAME_COLUMNS), received=len(rows))
    reasons: dict[int, tuple[str, str]] = {}

    def reject(i: int, code: str, why: str) -> None:
        reasons.setdefault(i, (code, why))

    good: list[tuple[int, dict[str, Any]]] = []
    for i, r in enumerate(rows):
        if isinstance(r, dict):
            good.append((i, r))
        else:
            reject(i, "MALFORMED_ROW", "row is not an object")
    now = pd.Timestamp(lk.reference_time())
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    oldest = now - pd.Timedelta(days=rules.max_age_days)
    newest = now + pd.Timedelta(hours=rules.max_future_hours)

    parsed: dict[int, dict[str, Any]] = {}
    for i, r in good:
        extra = sorted(str(k)[:40] for k in r if k not in TXN_FRAME_COLUMNS)
        if extra:
            reject(i, "MALFORMED_ROW", f"unexpected field(s): {', '.join(extra[:5])}")
            continue
        row = {c: (None if _is_blank(r.get(c)) else r.get(c)) for c in TXN_FRAME_COLUMNS}
        miss = [c for c in REQUIRED if row[c] is None]
        if miss:
            reject(i, "MISSING_FIELD", f"missing required field(s): {', '.join(miss)}")
            continue
        ts = pd.to_datetime(row["timestamp"], utc=True, errors="coerce")
        if pd.isna(ts):
            reject(i, "INVALID_TIMESTAMP", "timestamp cannot be parsed")
            continue
        if ts < oldest or ts > newest:
            reject(i, "TIMESTAMP_OUT_OF_RANGE",
                   f"timestamp outside [{rules.max_age_days} days before, {rules.max_future_hours} h after] "
                   "the reference time")
            continue
        try:
            amount = float(str(row["amount"])) if not isinstance(row["amount"], (int, float)) else float(row["amount"])
            if amount != amount or amount in (float("inf"), float("-inf")):
                raise ValueError
        except (TypeError, ValueError):
            reject(i, "INVALID_AMOUNT", "amount is not a finite number")
            continue
        if amount <= 0:
            reject(i, "NON_POSITIVE_AMOUNT", "amount must be greater than zero")
            continue
        cur = row["currency"]
        if not isinstance(cur, str) or not re.fullmatch(r"[A-Za-z]{3}", cur):
            reject(i, "INVALID_CURRENCY", "currency must be a 3-letter code")
            continue
        cur = cur.upper()
        if _fx(cur) is None:
            reject(i, "UNSUPPORTED_CURRENCY", f"currency {cur} is not configured")
            continue
        ttype = str(row["transaction_type"]).strip().lower()
        if ttype not in rules.transaction_types:
            reject(i, "INVALID_TYPE", f"transaction_type must be one of {rules.transaction_types}")
            continue
        channel = str(row["channel"]).strip().lower()
        if channel not in rules.channels:
            reject(i, "INVALID_CHANNEL", f"channel must be one of {rules.channels}")
            continue
        status = "completed" if row["status"] is None else str(row["status"]).strip().lower()
        if status not in rules.statuses:
            reject(i, "INVALID_STATUS", f"status must be one of {rules.statuses}")
            continue
        has_s, has_r = row["sender_account_id"] is not None, row["receiver_account_id"] is not None
        if not has_s and not has_r:
            reject(i, "NO_ACCOUNT", "a transaction needs a sender or a receiver account")
            continue
        need = TYPE_DIRECTION[ttype]
        if (need == "S" and not has_s) or (need == "R" and not has_r):
            reject(i, "DIRECTION_MISMATCH", f"a {ttype} needs a {'sender' if need == 'S' else 'receiver'} account")
            continue
        ip = row["ip_address"]
        if ip is not None:
            try:
                ipaddress.ip_address(str(ip))
            except ValueError:
                reject(i, "INVALID_IP", "ip_address is not a valid IPv4/IPv6 address")
                continue
        tid = row["transaction_id"]
        if tid is None and require_transaction_id:
            reject(i, "MISSING_TRANSACTION_ID", "transaction_id is required for this source")
            continue
        if tid is not None and not TXN_ID_RE.match(str(tid)):
            reject(i, "INVALID_ID_FORMAT", "transaction_id must look like TXN-<digits>")
            continue
        row.update(timestamp=ts, amount=amount, currency=cur, transaction_type=ttype, channel=channel, status=status)
        parsed[i] = row

    # referential integrity and account status
    accounts = sorted({str(row[c]) for row in parsed.values() for c in ("sender_account_id", "receiver_account_id")
                       if row[c] is not None})
    known = lk.owners(accounts) if accounts else {}
    status_of = lk.account_status(accounts) if accounts and rules.reject_closed_accounts else {}
    devs = sorted({str(r["device_id"]) for r in parsed.values() if r["device_id"] is not None})
    bad_dev = set(devs) - lk.known_devices(devs) if devs else set()
    mers = sorted({str(r["merchant_id"]) for r in parsed.values() if r["merchant_id"] is not None})
    bad_mer = set(mers) - lk.known_merchants(mers) if mers else set()
    for i, row in list(parsed.items()):
        flagged = False
        for col in ("sender_account_id", "receiver_account_id"):
            a = row[col]
            if a is None:
                continue
            if str(a) not in known:
                reject(i, "UNKNOWN_ACCOUNT", f"unknown {col} {a}")
                flagged = True
                break
            if status_of.get(str(a)) == "closed":
                reject(i, "ACCOUNT_CLOSED", f"{col} {a} belongs to a closed account")
                flagged = True
                break
        if flagged:
            continue
        if row["device_id"] is not None and str(row["device_id"]) in bad_dev:
            reject(i, "UNKNOWN_DEVICE", f"unknown device_id {row['device_id']}")
        elif row["merchant_id"] is not None and str(row["merchant_id"]) in bad_mer:
            reject(i, "UNKNOWN_MERCHANT", f"unknown merchant_id {row['merchant_id']}")

    # exact duplicates: inside the batch and against the store
    ids = [str(r["transaction_id"]) for i, r in parsed.items() if i not in reasons and r["transaction_id"] is not None]
    clash = lk.existing_ids(ids) if ids else set()
    first_seen: dict[str, int] = {}
    for i in sorted(parsed):
        if i in reasons:
            continue
        t = parsed[i]["transaction_id"]
        if t is None:
            continue
        t = str(t)
        if t in clash:
            reject(i, "DUPLICATE_ID", f"transaction_id {t} already exists")
        elif t in first_seen:
            reject(i, "DUPLICATE_ID", f"transaction_id {t} repeated in this batch (first at row {first_seen[t]})")
        else:
            first_seen[t] = i

    # likely duplicates: same parties, amount and currency within a short window, different (or no) id
    live = [(i, r) for i, r in sorted(parsed.items()) if i not in reasons]
    if live and rules.duplicate_policy == "reject":
        win = pd.Timedelta(seconds=rules.likely_duplicate_window_seconds)
        accs = sorted({str(r[c]) for _, r in live for c in ("sender_account_id", "receiver_account_id")
                       if r[c] is not None})
        t_lo = min(r["timestamp"] for _, r in live) - win
        t_hi = max(r["timestamp"] for _, r in live) + win
        recent = lk.recent_transactions(accs, t_lo, t_hi)
        seen: dict[tuple, list[pd.Timestamp]] = {}
        if recent is not None and len(recent):
            for t in recent.itertuples():
                key = (t.sender_account_id if isinstance(t.sender_account_id, str) else None,
                       t.receiver_account_id if isinstance(t.receiver_account_id, str) else None,
                       round(float(t.amount), 2), str(t.currency))
                seen.setdefault(key, []).append(pd.Timestamp(t.timestamp))
        for i, r in live:
            key = (r["sender_account_id"], r["receiver_account_id"], round(float(r["amount"]), 2), r["currency"])
            if any(abs(r["timestamp"] - ts) <= win for ts in seen.get(key, [])):
                reject(i, "LIKELY_DUPLICATE", f"same parties, amount and currency within "
                                              f"{rules.likely_duplicate_window_seconds}s of another transaction")
            else:
                seen.setdefault(key, []).append(r["timestamp"])

    for i in sorted(reasons):
        code, why = reasons[i]
        tid = rows[i].get("transaction_id") if isinstance(rows[i], dict) else None
        res.rejected.append({"row": i, "code": code, "group": code_group(code), "reason": why,
                             "transaction_id": None if _is_blank(tid) else str(tid)[:40],
                             "payload": sanitise_row(rows[i])})
        res.reason_counts[code] = res.reason_counts.get(code, 0) + 1

    ok = [(i, r) for i, r in sorted(parsed.items()) if i not in reasons]
    if not ok:
        return res
    df = pd.DataFrame([r for _, r in ok])[TXN_FRAME_COLUMNS].reset_index(drop=True)
    gen = df["transaction_id"].isna()
    if gen.any():
        base = uuid.uuid4().int % 10**9
        df.loc[gen, "transaction_id"] = [f"TXN-9{base + k:011d}"[:16] for k in range(int(gen.sum()))]
        res.ids_generated = int(gen.sum())
    fx = df["currency"].map(lambda c: _fx(str(c)))
    miss_usd = df["amount_usd"].isna()
    df.loc[miss_usd, "amount_usd"] = (df.loc[miss_usd, "amount"].astype(float) / fx[miss_usd]).round(2)
    df["amount"] = df["amount"].astype(float)
    df["amount_usd"] = pd.to_numeric(df["amount_usd"], errors="coerce").astype(float)
    # late arrival: older than the newest transaction already held, by more than the allowed lateness
    latest = lk.latest_timestamp()
    if latest is not None:
        cutoff = pd.Timestamp(latest)
        cutoff = (cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")) \
            - pd.Timedelta(hours=rules.late_after_hours)
        res.late_ids = [str(t) for t, ts in zip(df["transaction_id"], df["timestamp"], strict=False) if ts < cutoff]
    res.accepted = df
    return res


def missing_ids(expected: dict[str, Any] | None, received_ids: set[str], exists: Callable[[list[str]], set[str]],
                received_count: int) -> tuple[int | None, list[str]]:
    """Count (and sample) expected transactions that never arrived. `expected` may carry `count`, `ids` or `sequence`."""
    if not expected:
        return None, []
    ids: list[str] | None = None
    if expected.get("ids"):
        ids = [str(x) for x in expected["ids"]]
    elif expected.get("sequence"):
        s = expected["sequence"]
        ids = [f"{s.get('prefix', 'TXN-')}{n}" for n in range(int(s["start"]), int(s["end"]) + 1)]
    if ids is not None:
        absent = [i for i in ids if i not in received_ids]
        present = exists(absent[:50_000]) if absent else set()
        gone = [i for i in absent if i not in present]
        return len(gone), gone[:25]
    if expected.get("count") is not None:
        return max(int(expected["count"]) - received_count, 0), []
    return None, []
