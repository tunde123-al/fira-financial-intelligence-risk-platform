"""In-process DataStore backed by pandas DataFrames loaded from the synthetic dataset.

Used for offline evaluation (fast benchmark runs without infrastructure) and in
tests. Semantics mirror `SqlStore`; writes live in process memory only.
"""
from __future__ import annotations

import json
import re
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.data.store import TXN_FRAME_COLUMNS, utcnow
from app.schemas.domain import (
    Account,
    Alert,
    AuditEvent,
    Customer,
    CustomerIdentifier,
    Device,
    EvidenceItem,
    HumanDecision,
    Investigation,
    Merchant,
    SearchHit,
    Transaction,
)


def _ts(x: datetime | None) -> pd.Timestamp | None:
    if x is None:
        return None
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, float) and np.isnan(v):
            out[k] = None
        elif v is pd.NaT:
            out[k] = None
        elif isinstance(v, pd.Timestamp):
            out[k] = v.to_pydatetime(warn=False)
        elif isinstance(v, np.generic):
            out[k] = v.item()
        else:
            out[k] = v
    return out


class FrameStore:
    def __init__(self, dataset_dir: Path):
        self.dataset_dir = Path(dataset_dir)
        if not (self.dataset_dir / "manifest.json").exists():
            raise FileNotFoundError(f"No synthetic dataset in {self.dataset_dir}; run the generator first")
        self.manifest = json.loads((self.dataset_dir / "manifest.json").read_text(encoding="utf-8"))
        rd = lambda n: pd.read_csv(self.dataset_dir / f"{n}.csv.gz")  # noqa: E731
        self.customers = rd("customers")
        self.identifiers = rd("customer_identifiers")
        self.accounts = rd("accounts")
        self.merchants = rd("merchants")
        self.devices = rd("devices")
        tx = rd("transactions")
        alerts = rd("alerts")
        hist = rd("historical_investigations")
        for df, cols in ((self.customers, ["created_at"]), (self.accounts, ["opened_at"]),
                         (self.devices, ["first_seen", "last_seen"]), (tx, ["timestamp"]),
                         (alerts, ["created_at"]), (hist, ["created_at", "closed_at"])):
            for c in cols:
                df[c] = pd.to_datetime(df[c], utc=True, format="ISO8601")
        tx = tx[TXN_FRAME_COLUMNS]
        self.tx = tx
        self.acc_owner = self.accounts.set_index("account_id").customer_id
        self._reindex_transactions()
        self._cust = self.customers.set_index("customer_id", drop=False)
        self._acc = self.accounts.set_index("account_id", drop=False)
        self._mer = self.merchants.set_index("merchant_id", drop=False)
        self._dev = self.devices.set_index("device_id", drop=False)
        self.alerts = alerts
        self._lock = threading.RLock()
        self._investigations: dict[str, Investigation] = {}
        for r in hist.to_dict("records"):
            r = _clean(r)
            sig = r.pop("signals", "[]")
            signals = _parse_list(sig)
            self._investigations[r["investigation_id"]] = Investigation(
                investigation_id=r["investigation_id"], subject_id=r["subject_id"],
                subject_type=r["subject_type"], assigned_to=r.get("assigned_to"), status=r["status"],
                created_at=r["created_at"], closed_at=r.get("closed_at"), conclusion=r.get("conclusion"),
                signals=signals, summary=r.get("summary"))
        self._evidence: dict[str, list[EvidenceItem]] = {}
        self._episodes: dict[str, dict[str, Any]] = {}
        self._decisions: list[HumanDecision] = []
        self._audit: list[AuditEvent] = []
        self._configs: dict[str, dict[str, Any]] = {}
        self._evals: list[dict[str, Any]] = []
        self._users: dict[str, dict[str, Any]] = {}
        self._peer_cache: dict[tuple, dict[str, float]] = {}

    def _reindex_transactions(self) -> None:
        """(Re)build the per-account / per-device position indices after the frame changed."""
        tx = self.tx
        self._by_sender = tx.groupby("sender_account_id").indices
        self._by_receiver = tx.groupby("receiver_account_id").indices
        self._by_device = tx.groupby("device_id").indices
        self._txi = tx.set_index("transaction_id", drop=False)
        self._peer_cache = {}

    # ------------------------------------------------------------------ basics
    def as_of(self) -> datetime:
        return pd.Timestamp(self.manifest["as_of"]).to_pydatetime()

    def ping(self) -> bool:
        return True

    def list_customer_ids(self) -> list[str]:
        return self.customers.customer_id.tolist()

    def get_customer(self, customer_id: str) -> Customer | None:
        if customer_id not in self._cust.index:
            return None
        return Customer(**_clean(self._cust.loc[customer_id].to_dict()))

    def get_identifiers(self, customer_id: str) -> list[CustomerIdentifier]:
        rows = self.identifiers[self.identifiers.customer_id == customer_id]
        return [CustomerIdentifier(**_clean(r)) for r in rows.drop(columns=["customer_id"]).to_dict("records")]

    def customers_sharing_identifiers(self, customer_id: str) -> list[dict[str, Any]]:
        mine = self.identifiers[self.identifiers.customer_id == customer_id]
        if mine.empty:
            return []
        m = self.identifiers.merge(mine[["identifier_type", "value_hash"]], on=["identifier_type", "value_hash"])
        m = m[m.customer_id != customer_id]
        return [{"identifier_type": r.identifier_type, "masked_value": r.masked_value,
                 "customer_id": r.customer_id} for r in m.itertuples()]

    def get_account(self, account_id: str) -> Account | None:
        if account_id not in self._acc.index:
            return None
        return Account(**_clean(self._acc.loc[account_id].to_dict()))

    def accounts_for_customer(self, customer_id: str) -> list[Account]:
        rows = self.accounts[self.accounts.customer_id == customer_id]
        return [Account(**_clean(r)) for r in rows.to_dict("records")]

    def owners_of_accounts(self, account_ids: list[str]) -> dict[str, str]:
        s = self.acc_owner.reindex([a for a in account_ids if a]).dropna()
        return s.to_dict()

    def get_transaction(self, transaction_id: str) -> Transaction | None:
        if transaction_id not in self._txi.index:
            return None
        return Transaction(**_clean(self._txi.loc[transaction_id].to_dict()))

    def get_merchant(self, merchant_id: str) -> Merchant | None:
        if merchant_id not in self._mer.index:
            return None
        return Merchant(**_clean(self._mer.loc[merchant_id].to_dict()))

    def get_merchants(self, merchant_ids: list[str]) -> list[Merchant]:
        ids = [m for m in set(merchant_ids) if m in self._mer.index]
        return [Merchant(**_clean(r)) for r in self._mer.loc[ids].to_dict("records")] if ids else []

    def get_device(self, device_id: str) -> Device | None:
        if device_id not in self._dev.index:
            return None
        return Device(**_clean(self._dev.loc[device_id].to_dict()))

    def search(self, query: str, entity_type: str | None = None, limit: int = 20) -> list[SearchHit]:
        q = query.strip().upper()
        hits: list[SearchHit] = []
        if not q:
            return hits

        def by_prefix(df: pd.DataFrame, col: str, et: str, label_fn) -> None:
            if entity_type and entity_type != et:
                return
            m = df[df[col].str.upper().str.startswith(q)].head(limit)
            for r in m.to_dict("records"):
                hits.append(SearchHit(entity_type=et, entity_id=r[col], label=label_fn(r)))

        by_prefix(self.customers, "customer_id", "customer",
                  lambda r: f"{r['customer_id']} · {r['customer_type']}/{r['segment']} · {r['home_city']}")
        by_prefix(self.accounts, "account_id", "account",
                  lambda r: f"{r['account_id']} · {r['account_type']} {r['currency']} · {r['customer_id']}")
        by_prefix(self.devices, "device_id", "device", lambda r: f"{r['device_id']} · {r['device_type']}")
        by_prefix(self.merchants, "merchant_id", "merchant",
                  lambda r: f"{r['merchant_id']} · {r['name']} · {r['category']}")
        if (not entity_type or entity_type == "merchant") and len(q) >= 3 and not q.startswith("MER-"):
            m = self.merchants[self.merchants.name.str.upper().str.contains(re.escape(q))].head(limit)
            for r in m.to_dict("records"):
                hits.append(SearchHit(entity_type="merchant", entity_id=r["merchant_id"],
                                      label=f"{r['merchant_id']} · {r['name']} · {r['category']}"))
        if (not entity_type or entity_type == "transaction") and q.startswith("TXN-"):
            if q in self._txi.index:
                r = self._txi.loc[q]
                hits.append(SearchHit(entity_type="transaction", entity_id=q,
                                      label=f"{q} · {r.transaction_type} {r.amount:.2f} {r.currency}"))
        return hits[:limit]

    # ------------------------------------------------------------ transactions
    def _window(self, df: pd.DataFrame, start: datetime | None, end: datetime | None) -> pd.DataFrame:
        s, e = _ts(start), _ts(end)
        if s is not None:
            df = df[df.timestamp >= s]
        if e is not None:
            df = df[df.timestamp <= e]
        return df

    def transactions_for_accounts(self, account_ids: list[str], start: datetime | None,
                                  end: datetime | None) -> pd.DataFrame:
        idx: list[int] = []
        for a in account_ids:
            idx.extend(self._by_sender.get(a, []))
            idx.extend(self._by_receiver.get(a, []))
        if not idx:
            return self.tx.iloc[0:0].copy()
        df = self.tx.iloc[sorted(set(idx))]
        return self._window(df, start, end).sort_values("timestamp").reset_index(drop=True)

    def transactions_for_device(self, device_id: str, start: datetime | None, end: datetime | None,
                                limit: int = 500) -> pd.DataFrame:
        idx = self._by_device.get(device_id, [])
        df = self.tx.iloc[list(idx)] if len(idx) else self.tx.iloc[0:0]
        return self._window(df, start, end).sort_values("timestamp").head(limit).reset_index(drop=True)

    def device_users(self, device_ids: list[str], start: datetime | None, end: datetime | None) -> pd.DataFrame:
        idx: list[int] = []
        for d in device_ids:
            idx.extend(self._by_device.get(d, []))
        cols = ["device_id", "customer_id", "n_transactions", "first_ts", "last_ts"]
        if not idx:
            return pd.DataFrame(columns=cols)
        df = self._window(self.tx.iloc[idx], start, end)
        df = df.assign(customer_id=df.sender_account_id.map(self.acc_owner))
        df = df.dropna(subset=["customer_id"])
        g = df.groupby(["device_id", "customer_id"]).timestamp.agg(["count", "min", "max"]).reset_index()
        g.columns = cols
        return g

    def peer_amount_stats(self, segment: str, start: datetime, end: datetime) -> dict[str, float]:
        key = (segment, str(start)[:10], str(end)[:10])
        if key in self._peer_cache:
            return self._peer_cache[key]
        custs = set(self.customers[self.customers.segment == segment].customer_id)
        accs = self.accounts[self.accounts.customer_id.isin(custs)].account_id
        df = self._window(self.tx[self.tx.sender_account_id.isin(accs)], start, end)
        df = df[df.status == "completed"]
        out = {"n": float(len(df))}
        if len(df):
            q = df.amount_usd.quantile([0.5, 0.95, 0.99])
            out.update({"p50_usd": float(q.loc[0.5]), "p95_usd": float(q.loc[0.95]), "p99_usd": float(q.loc[0.99])})
        self._peer_cache[key] = out
        return out

    def graph_edges(self, start: datetime | None, end: datetime | None) -> dict[str, pd.DataFrame]:
        tx = self._window(self.tx, start, end)
        tx = tx[tx.status == "completed"]
        transfers = tx.dropna(subset=["sender_account_id", "receiver_account_id"])
        tr = transfers.groupby(["sender_account_id", "receiver_account_id"]).agg(
            n=("transaction_id", "count"), total_usd=("amount_usd", "sum"),
            first_ts=("timestamp", "min"), last_ts=("timestamp", "max"),
            sample_txn_ids=("transaction_id", lambda s: list(s)[:5])).reset_index()
        dev = tx.dropna(subset=["device_id"]).copy()
        dev["customer_id"] = dev.sender_account_id.map(self.acc_owner)
        du = dev.dropna(subset=["customer_id"]).groupby(["customer_id", "device_id"]).agg(
            n=("transaction_id", "count"), first_ts=("timestamp", "min"), last_ts=("timestamp", "max")).reset_index()
        ip = dev.dropna(subset=["customer_id", "ip_address"]).groupby(["customer_id", "ip_address"]).agg(
            n=("transaction_id", "count")).reset_index()
        mp = tx.dropna(subset=["sender_account_id", "merchant_id"]).groupby(["sender_account_id", "merchant_id"]).agg(
            n=("transaction_id", "count"), total_usd=("amount_usd", "sum")).reset_index()
        return {
            "ownership": self.accounts[["customer_id", "account_id"]].copy(),
            "transfers": tr,
            "device_usage": du,
            "ip_usage": ip,
            "merchant_payments": mp.rename(columns={"sender_account_id": "account_id"}),
            "identifiers": self.identifiers[["customer_id", "identifier_type", "value_hash", "masked_value"]].copy(),
            "customers": self.customers[["customer_id", "customer_type", "segment", "risk_profile", "country"]].copy(),
            "merchants": self.merchants[["merchant_id", "name", "category", "risk_profile", "country"]].copy(),
            "alerts": self.alerts[["entity_id", "entity_type", "status", "created_at"]].copy(),
        }

    # ------------------------------------------------------- monitoring support
    def active_customers(self, start: datetime | None, end: datetime | None) -> list[str]:
        df = self._window(self.tx, start, end)
        accs = pd.concat([df.sender_account_id.dropna(), df.receiver_account_id.dropna()]).unique()
        return sorted(self.acc_owner.reindex(accs).dropna().unique().tolist())

    def count_transactions(self, start: datetime | None, end: datetime | None) -> int:
        return int(len(self._window(self.tx, start, end)))

    def known_transaction_ids(self, ids: list[str]) -> set[str]:
        return {i for i in ids if i in self._txi.index}

    def known_device_ids(self, ids: list[str]) -> set[str]:
        return {i for i in ids if i in self._dev.index}

    def known_merchant_ids(self, ids: list[str]) -> set[str]:
        return {i for i in ids if i in self._mer.index}

    def account_statuses(self, account_ids: list[str]) -> dict[str, str]:
        s = self._acc["status"].reindex([a for a in account_ids if a]).dropna()
        return {str(k): str(v) for k, v in s.items()}

    def dataset_manifest(self) -> dict[str, Any]:
        return dict(self.manifest)

    def latest_transaction_time(self) -> datetime | None:
        if self.tx.empty:
            return None
        return pd.Timestamp(self.tx["timestamp"].max()).to_pydatetime(warn=False)

    def ingest_transactions(self, df: pd.DataFrame) -> int:
        """Append already-validated rows (see `app.monitoring.ingest`). Returns the rows added."""
        if df.empty:
            return 0
        with self._lock:
            add = df.reindex(columns=TXN_FRAME_COLUMNS).copy()
            add["timestamp"] = pd.to_datetime(add["timestamp"], utc=True)
            self.tx = pd.concat([self.tx, add], ignore_index=True).sort_values(
                "timestamp", kind="mergesort").reset_index(drop=True)
            self._reindex_transactions()
        return int(len(df))

    # ------------------------------------------------------------ alerts etc.
    def list_alerts(self, entity_id: str | None = None, status: str | None = None,
                    before: datetime | None = None, limit: int = 100) -> list[Alert]:
        df = self.alerts
        if entity_id:
            df = df[df.entity_id == entity_id]
        if status:
            df = df[df.status == status]
        if before is not None:
            df = df[df.created_at < _ts(before)]
        df = df.sort_values("created_at", ascending=False).head(limit)
        return [Alert(**_clean(r)) for r in df.to_dict("records")]

    def alert_counts(self) -> dict[str, int]:
        open_ = self.alerts[self.alerts.status == "open"]
        return {str(k): int(v) for k, v in open_.severity.value_counts().items()}

    def list_investigations(self, subject_ids: list[str] | None = None, status: str | None = None,
                            limit: int = 100) -> list[Investigation]:
        with self._lock:
            items = list(self._investigations.values())
        if subject_ids is not None:
            s = set(subject_ids)
            items = [i for i in items if i.subject_id in s]
        if status:
            items = [i for i in items if i.status == status]
        items.sort(key=lambda i: i.created_at, reverse=True)
        return [i.model_copy(deep=True) for i in items[:limit]]

    def get_investigation(self, investigation_id: str) -> Investigation | None:
        with self._lock:
            inv = self._investigations.get(investigation_id)
            return inv.model_copy(deep=True) if inv else None

    def create_investigation(self, inv: Investigation) -> Investigation:
        with self._lock:
            self._investigations[inv.investigation_id] = inv.model_copy(deep=True)
        return inv

    def update_investigation(self, investigation_id: str, **fields: Any) -> Investigation | None:
        with self._lock:
            inv = self._investigations.get(investigation_id)
            if not inv:
                return None
            updated = inv.model_copy(update=fields, deep=True)
            self._investigations[investigation_id] = updated
            return updated.model_copy(deep=True)

    def add_evidence(self, items: list[EvidenceItem]) -> None:
        with self._lock:
            for it in items:
                self._evidence.setdefault(it.investigation_id or "", []).append(it.model_copy(deep=True))

    def list_evidence(self, investigation_id: str) -> list[EvidenceItem]:
        with self._lock:
            return [e.model_copy(deep=True) for e in self._evidence.get(investigation_id, [])]

    # --------------------------------------------------------------- episodes
    def save_episode(self, episode: dict[str, Any]) -> None:
        with self._lock:
            self._episodes[episode["episode_id"]] = deepcopy(episode)

    def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        with self._lock:
            e = self._episodes.get(episode_id)
            return deepcopy(e) if e else None

    def episodes_for_investigation(self, investigation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [deepcopy(e) for e in self._episodes.values() if e.get("investigation_id") == investigation_id]

    def list_episodes(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            eps = sorted(self._episodes.values(), key=lambda e: str(e.get("started_at")), reverse=True)
            return [deepcopy(e) for e in eps[:limit]]

    def update_episode(self, episode_id: str, **fields: Any) -> None:
        with self._lock:
            if episode_id in self._episodes:
                self._episodes[episode_id].update(deepcopy(fields))

    def add_decision(self, decision: HumanDecision) -> None:
        with self._lock:
            self._decisions.append(decision.model_copy(deep=True))

    def list_decisions(self, investigation_id: str | None = None, limit: int = 500) -> list[HumanDecision]:
        with self._lock:
            ds = [d for d in self._decisions if investigation_id is None or d.investigation_id == investigation_id]
        return ds[-limit:]

    def append_audit(self, event: AuditEvent) -> None:
        with self._lock:
            event = event.model_copy(update={"id": len(self._audit) + 1})
            self._audit.append(event)

    def list_audit(self, limit: int = 200, user_id: str | None = None, action: str | None = None) -> list[AuditEvent]:
        with self._lock:
            ev = [e for e in self._audit if (not user_id or e.user_id == user_id) and (not action or e.action == action)]
        return list(reversed(ev))[:limit]

    def save_config_version(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._configs[record["version_id"]] = deepcopy(record)

    def list_config_versions(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted((deepcopy(v) for v in self._configs.values()), key=lambda r: str(r["created_at"]), reverse=True)

    def update_config_version(self, version_id: str, **fields: Any) -> None:
        with self._lock:
            if version_id in self._configs:
                self._configs[version_id].update(deepcopy(fields))

    def save_evaluation_run(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._evals.append(deepcopy(record))

    def list_evaluation_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [deepcopy(r) for r in reversed(self._evals)][:limit]

    def get_user(self, username: str) -> dict[str, Any] | None:
        with self._lock:
            u = self._users.get(username)
            return dict(u) if u else None

    def upsert_user(self, user: dict[str, Any]) -> None:
        with self._lock:
            self._users[user["username"]] = dict(user)

    def dashboard_stats(self) -> dict[str, Any]:
        invs = self.list_investigations(limit=100000)
        by_status: dict[str, int] = {}
        for i in invs:
            by_status[i.status] = by_status.get(i.status, 0) + 1
        scores = [i.risk_score for i in invs if i.risk_score is not None]
        return {
            "counts": {"customers": len(self.customers), "accounts": len(self.accounts),
                       "transactions": len(self.tx), "merchants": len(self.merchants), "devices": len(self.devices)},
            "open_alerts_by_severity": self.alert_counts(),
            "investigations_by_status": by_status,
            "risk_distribution": _histogram(scores),
            "as_of": self.as_of().isoformat(),
            "generated_at": utcnow().isoformat(),
        }


def _histogram(scores: list[float]) -> list[dict[str, Any]]:
    bins = [0, 20, 40, 60, 80, 101]
    out = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        out.append({"bucket": f"{lo}-{min(hi, 100)}", "count": sum(1 for s in scores if lo <= s < hi)})
    return out


def _parse_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    if not isinstance(v, str) or not v:
        return []
    try:
        import ast
        parsed = ast.literal_eval(v)
        return [str(x) for x in parsed] if isinstance(parsed, (list, tuple)) else []
    except (ValueError, SyntaxError):
        return []
