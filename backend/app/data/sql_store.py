"""PostgreSQL DataStore (SQLAlchemy Core + psycopg 3).

All statements use bound parameters (`:name`) — no string interpolation of user
input — which is the SQL-injection boundary. The only dynamic SQL is the choice
among fixed, code-defined fragments (optional time filters).

The SQL text lives in module-level constants so it can be reviewed in one place
and exercised directly by `tests/integration/test_sql_statements.py`.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from typing import Any

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

TXN_COLS_SQL = ", ".join(f't."{c}"' if c == "timestamp" else f"t.{c}" for c in TXN_FRAME_COLUMNS).replace(
    "t.ip_address", "host(t.ip_address) AS ip_address")

SQL = {
    "as_of": "SELECT (manifest->>'as_of')::timestamptz AS as_of FROM dataset_manifest WHERE id = 1",
    "max_ts": 'SELECT max("timestamp") AS as_of FROM transactions',
    "customer_ids": "SELECT customer_id FROM customers ORDER BY customer_id",
    "customer": "SELECT * FROM customers WHERE customer_id = :id",
    "identifiers": "SELECT identifier_type, masked_value, value_hash FROM customer_identifiers WHERE customer_id = :id",
    "shared_identifiers": (
        "SELECT o.identifier_type, o.masked_value, o.customer_id FROM customer_identifiers m "
        "JOIN customer_identifiers o ON o.identifier_type = m.identifier_type AND o.value_hash = m.value_hash "
        "AND o.customer_id <> m.customer_id WHERE m.customer_id = :id"),
    "account": "SELECT account_id, customer_id, account_type, currency, opened_at, status, balance::float8 AS balance "
               "FROM accounts WHERE account_id = :id",
    "accounts_for_customer": "SELECT account_id, customer_id, account_type, currency, opened_at, status, "
                             "balance::float8 AS balance FROM accounts WHERE customer_id = :id ORDER BY account_id",
    "owners": "SELECT account_id, customer_id FROM accounts WHERE account_id = ANY(:ids)",
    "transaction": f"SELECT {TXN_COLS_SQL} FROM transactions t WHERE t.transaction_id = :id",
    "merchant": "SELECT merchant_id, name, category, country, city, latitude, longitude, risk_profile "
                "FROM merchants WHERE merchant_id = :id",
    "merchants": "SELECT merchant_id, name, category, country, city, latitude, longitude, risk_profile "
                 "FROM merchants WHERE merchant_id = ANY(:ids)",
    "device": "SELECT * FROM devices WHERE device_id = :id",
    # transactions touching a set of accounts; UNION keeps both index paths usable
    "txns_for_accounts": (
        f"SELECT {TXN_COLS_SQL} FROM transactions t WHERE t.sender_account_id = ANY(:ids) "
        'AND t."timestamp" >= :start AND t."timestamp" <= :end '
        f"UNION SELECT {TXN_COLS_SQL} FROM transactions t WHERE t.receiver_account_id = ANY(:ids) "
        'AND t."timestamp" >= :start AND t."timestamp" <= :end ORDER BY 2'),
    "txns_for_device": (f"SELECT {TXN_COLS_SQL} FROM transactions t WHERE t.device_id = :id "
                        'AND t."timestamp" >= :start AND t."timestamp" <= :end ORDER BY t."timestamp" LIMIT :limit'),
    "device_users": (
        'SELECT t.device_id, a.customer_id, count(*) AS n_transactions, min(t."timestamp") AS first_ts, '
        'max(t."timestamp") AS last_ts FROM transactions t JOIN accounts a ON a.account_id = t.sender_account_id '
        'WHERE t.device_id = ANY(:ids) AND t."timestamp" >= :start AND t."timestamp" <= :end '
        "GROUP BY t.device_id, a.customer_id"),
    "peer_stats": (
        "SELECT count(*) AS n, percentile_cont(0.5) WITHIN GROUP (ORDER BY t.amount_usd) AS p50_usd, "
        "percentile_cont(0.95) WITHIN GROUP (ORDER BY t.amount_usd) AS p95_usd, "
        "percentile_cont(0.99) WITHIN GROUP (ORDER BY t.amount_usd) AS p99_usd "
        "FROM transactions t JOIN accounts a ON a.account_id = t.sender_account_id "
        "JOIN customers c ON c.customer_id = a.customer_id "
        "WHERE c.segment = :segment AND t.status = 'completed' AND t.\"timestamp\" >= :start AND t.\"timestamp\" < :end"),
    "g_ownership": "SELECT customer_id, account_id FROM accounts",
    "g_transfers": (
        "SELECT sender_account_id, receiver_account_id, count(*) AS n, sum(amount_usd)::float8 AS total_usd, "
        'min("timestamp") AS first_ts, max("timestamp") AS last_ts, '
        '(array_agg(transaction_id ORDER BY "timestamp"))[1:5] AS sample_txn_ids '
        "FROM transactions WHERE status = 'completed' AND sender_account_id IS NOT NULL "
        'AND receiver_account_id IS NOT NULL AND "timestamp" >= :start AND "timestamp" <= :end '
        "GROUP BY sender_account_id, receiver_account_id"),
    "g_device_usage": (
        'SELECT a.customer_id, t.device_id, count(*) AS n, min(t."timestamp") AS first_ts, max(t."timestamp") AS last_ts '
        "FROM transactions t JOIN accounts a ON a.account_id = t.sender_account_id "
        "WHERE t.status = 'completed' AND t.device_id IS NOT NULL "
        'AND t."timestamp" >= :start AND t."timestamp" <= :end GROUP BY a.customer_id, t.device_id'),
    "g_ip_usage": (
        "SELECT a.customer_id, host(t.ip_address) AS ip_address, count(*) AS n "
        "FROM transactions t JOIN accounts a ON a.account_id = t.sender_account_id "
        "WHERE t.status = 'completed' AND t.ip_address IS NOT NULL "
        'AND t."timestamp" >= :start AND t."timestamp" <= :end GROUP BY a.customer_id, host(t.ip_address)'),
    "g_merchant_payments": (
        "SELECT sender_account_id AS account_id, merchant_id, count(*) AS n, sum(amount_usd)::float8 AS total_usd "
        "FROM transactions WHERE status = 'completed' AND sender_account_id IS NOT NULL AND merchant_id IS NOT NULL "
        'AND "timestamp" >= :start AND "timestamp" <= :end GROUP BY sender_account_id, merchant_id'),
    "g_identifiers": "SELECT customer_id, identifier_type, value_hash, masked_value FROM customer_identifiers",
    "g_customers": "SELECT customer_id, customer_type, segment, risk_profile, country FROM customers",
    "g_merchants": "SELECT merchant_id, name, category, risk_profile, country FROM merchants",
    "g_alerts": "SELECT entity_id, entity_type, status, created_at FROM alerts",
    "search_customer": ("SELECT customer_id, customer_type, segment, home_city FROM customers "
                        "WHERE customer_id LIKE :prefix ORDER BY customer_id LIMIT :limit"),
    "search_account": ("SELECT account_id, account_type, currency, customer_id FROM accounts "
                       "WHERE account_id LIKE :prefix ORDER BY account_id LIMIT :limit"),
    "search_device": ("SELECT device_id, device_type FROM devices WHERE device_id LIKE :prefix "
                      "ORDER BY device_id LIMIT :limit"),
    "search_merchant": ("SELECT merchant_id, name, category FROM merchants WHERE merchant_id LIKE :prefix "
                        "OR lower(name) LIKE :contains ESCAPE '\\' ORDER BY merchant_id LIMIT :limit"),
    "search_transaction": ("SELECT transaction_id, transaction_type, amount::float8 AS amount, currency "
                           "FROM transactions WHERE transaction_id = :exact"),
    "alerts": ("SELECT alert_id, entity_id, entity_type, alert_type, severity, risk_score, created_at, status "
               "FROM alerts WHERE (:entity_id = '' OR entity_id = :entity_id) AND (:status = '' OR status = :status) "
               "AND created_at < :before ORDER BY created_at DESC LIMIT :limit"),
    "alert_counts": "SELECT severity, count(*) AS n FROM alerts WHERE status = 'open' GROUP BY severity",
    "investigations": ("SELECT * FROM investigations WHERE (:all_subjects OR subject_id = ANY(:subjects)) "
                       "AND (:status = '' OR status = :status) ORDER BY created_at DESC LIMIT :limit"),
    "investigation": "SELECT * FROM investigations WHERE investigation_id = :id",
    "insert_investigation": (
        "INSERT INTO investigations (investigation_id, subject_id, subject_type, assigned_to, status, created_at, "
        "closed_at, conclusion, request_text, risk_score, created_by, signals, summary, report) VALUES "
        "(:investigation_id, :subject_id, :subject_type, :assigned_to, :status, :created_at, :closed_at, :conclusion, "
        ":request_text, :risk_score, :created_by, :signals, :summary, CAST(:report AS jsonb))"),
    "insert_evidence": (
        "INSERT INTO evidence (evidence_id, investigation_id, ref, source_type, source_id, evidence_type, title, "
        "content, confidence, created_at) VALUES (:evidence_id, :investigation_id, :ref, :source_type, :source_id, "
        ":evidence_type, :title, CAST(:content AS jsonb), :confidence, :created_at) "
        "ON CONFLICT (investigation_id, ref) DO UPDATE SET content = EXCLUDED.content, title = EXCLUDED.title"),
    "evidence": "SELECT * FROM evidence WHERE investigation_id = :id ORDER BY created_at, ref",
    "insert_episode": (
        "INSERT INTO agent_episodes (episode_id, investigation_id, request, requested_by, status, plan, tool_calls, "
        "observations, retrieved_evidence, reasoning_summary, final_output, evaluation, human_feedback, model, "
        "tokens_in, tokens_out, latency_ms, started_at, finished_at) VALUES (:episode_id, :investigation_id, :request, "
        ":requested_by, :status, CAST(:plan AS jsonb), CAST(:tool_calls AS jsonb), CAST(:observations AS jsonb), "
        "CAST(:retrieved_evidence AS jsonb), :reasoning_summary, CAST(:final_output AS jsonb), "
        "CAST(:evaluation AS jsonb), CAST(:human_feedback AS jsonb), :model, :tokens_in, :tokens_out, :latency_ms, "
        ":started_at, :finished_at) ON CONFLICT (episode_id) DO UPDATE SET status = EXCLUDED.status, "
        "plan = EXCLUDED.plan, tool_calls = EXCLUDED.tool_calls, observations = EXCLUDED.observations, "
        "retrieved_evidence = EXCLUDED.retrieved_evidence, reasoning_summary = EXCLUDED.reasoning_summary, "
        "final_output = EXCLUDED.final_output, evaluation = EXCLUDED.evaluation, "
        "human_feedback = EXCLUDED.human_feedback, model = EXCLUDED.model, tokens_in = EXCLUDED.tokens_in, "
        "tokens_out = EXCLUDED.tokens_out, latency_ms = EXCLUDED.latency_ms, finished_at = EXCLUDED.finished_at"),
    "episode": "SELECT * FROM agent_episodes WHERE episode_id = :id",
    "episodes_for_investigation": "SELECT * FROM agent_episodes WHERE investigation_id = :id ORDER BY started_at",
    "episodes": "SELECT * FROM agent_episodes ORDER BY started_at DESC LIMIT :limit",
    "insert_decision": (
        "INSERT INTO human_decisions (decision_id, investigation_id, decided_by, decision, rationale, "
        "failure_categories, report_quality, created_at) VALUES (:decision_id, :investigation_id, :decided_by, "
        ":decision, :rationale, :failure_categories, :report_quality, :created_at)"),
    "decisions": ("SELECT * FROM human_decisions WHERE (:all OR investigation_id = :id) "
                  "ORDER BY created_at DESC LIMIT :limit"),
    "insert_audit": (
        "INSERT INTO audit_log (ts, user_id, role, action, tool, entity_type, entity_id, result, request_id, details) "
        "VALUES (:ts, :user_id, :role, :action, :tool, :entity_type, :entity_id, :result, :request_id, "
        "CAST(:details AS jsonb))"),
    "audit": ("SELECT * FROM audit_log WHERE (:user_id = '' OR user_id = :user_id) AND (:action = '' OR action = :action) "
              "ORDER BY id DESC LIMIT :limit"),
    "insert_config": (
        "INSERT INTO risk_config_versions (version_id, created_at, created_by, status, rationale, config, evaluation, "
        "approved_by, approved_at) VALUES (:version_id, :created_at, :created_by, :status, :rationale, "
        "CAST(:config AS jsonb), CAST(:evaluation AS jsonb), :approved_by, :approved_at)"),
    "configs": "SELECT * FROM risk_config_versions ORDER BY created_at DESC",
    "insert_eval": ("INSERT INTO evaluation_runs (run_id, created_at, kind, config_version, metrics, details) VALUES "
                    "(:run_id, :created_at, :kind, :config_version, CAST(:metrics AS jsonb), CAST(:details AS jsonb))"),
    "evals": "SELECT * FROM evaluation_runs ORDER BY created_at DESC LIMIT :limit",
    "user": "SELECT * FROM users WHERE username = :username",
    "upsert_user": (
        "INSERT INTO users (user_id, username, password_hash, role, active) VALUES (:user_id, :username, "
        ":password_hash, :role, :active) ON CONFLICT (username) DO UPDATE SET password_hash = EXCLUDED.password_hash, "
        "role = EXCLUDED.role, active = EXCLUDED.active"),
    "counts": ("SELECT (SELECT count(*) FROM customers) AS customers, (SELECT count(*) FROM accounts) AS accounts, "
               "(SELECT count(*) FROM transactions) AS transactions, (SELECT count(*) FROM merchants) AS merchants, "
               "(SELECT count(*) FROM devices) AS devices"),
    "inv_by_status": "SELECT status, count(*) AS n FROM investigations GROUP BY status",
    "risk_hist": ("SELECT width_bucket(risk_score, 0, 100, 5) AS b, count(*) AS n FROM investigations "
                  "WHERE risk_score IS NOT NULL GROUP BY 1 ORDER BY 1"),
}

# Allowed columns for update_* helpers (whitelist -> no identifier injection).
INVESTIGATION_UPDATABLE = {"assigned_to", "status", "closed_at", "conclusion", "risk_score", "signals", "summary", "report"}
EPISODE_UPDATABLE = {"status", "evaluation", "human_feedback", "final_output", "finished_at"}
CONFIG_UPDATABLE = {"status", "evaluation", "approved_by", "approved_at", "rationale"}
JSON_COLUMNS = {"report", "evaluation", "human_feedback", "final_output", "plan", "tool_calls", "observations",
                "retrieved_evidence", "content", "details", "config", "metrics"}

FAR_PAST = datetime(1900, 1, 1)
FAR_FUTURE = datetime(2999, 1, 1)


def _j(v: Any) -> str | None:
    return None if v is None else json.dumps(v, default=str)


def _utc_frame(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], utc=True)
    return df


class SqlStore:
    def __init__(self, database_url: str, echo: bool = False):
        from sqlalchemy import create_engine

        self.engine = create_engine(database_url, pool_pre_ping=True, pool_size=5, max_overflow=10,
                                    echo=echo, future=True)
        self._peer_cache: dict[tuple, dict[str, float]] = {}
        self._as_of: datetime | None = None
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- helpers
    def _rows(self, key: str, **params: Any) -> list[dict[str, Any]]:
        from sqlalchemy import text

        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(text(SQL[key]), params)]

    def _one(self, key: str, **params: Any) -> dict[str, Any] | None:
        rows = self._rows(key, **params)
        return rows[0] if rows else None

    def _exec(self, sql: str, **params: Any) -> None:
        from sqlalchemy import text

        with self.engine.begin() as conn:
            conn.execute(text(sql), params)

    def _frame(self, key: str, **params: Any) -> pd.DataFrame:
        from sqlalchemy import text

        with self.engine.connect() as conn:
            return pd.read_sql(text(SQL[key]), conn, params=params)

    def _txn_frame(self, key: str, **params: Any) -> pd.DataFrame:
        df = self._frame(key, **params)
        df = _utc_frame(df, ["timestamp"])
        for c in ("amount", "amount_usd", "latitude", "longitude"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
        return df.reindex(columns=TXN_FRAME_COLUMNS)

    # ------------------------------------------------------------------ basics
    def ping(self) -> bool:
        from sqlalchemy import text

        with self.engine.connect() as conn:
            return conn.execute(text("SELECT 1")).scalar() == 1

    def as_of(self) -> datetime:
        if self._as_of is None:
            row = self._one("as_of") or self._one("max_ts")
            self._as_of = row["as_of"] if row and row.get("as_of") else utcnow()
        return self._as_of

    def list_customer_ids(self) -> list[str]:
        return [r["customer_id"] for r in self._rows("customer_ids")]

    def get_customer(self, customer_id: str) -> Customer | None:
        r = self._one("customer", id=customer_id)
        return Customer(**r) if r else None

    def get_identifiers(self, customer_id: str) -> list[CustomerIdentifier]:
        return [CustomerIdentifier(**r) for r in self._rows("identifiers", id=customer_id)]

    def customers_sharing_identifiers(self, customer_id: str) -> list[dict[str, Any]]:
        return self._rows("shared_identifiers", id=customer_id)

    def get_account(self, account_id: str) -> Account | None:
        r = self._one("account", id=account_id)
        return Account(**r) if r else None

    def accounts_for_customer(self, customer_id: str) -> list[Account]:
        return [Account(**r) for r in self._rows("accounts_for_customer", id=customer_id)]

    def owners_of_accounts(self, account_ids: list[str]) -> dict[str, str]:
        return {r["account_id"]: r["customer_id"] for r in self._rows("owners", ids=list(account_ids))}

    def get_transaction(self, transaction_id: str) -> Transaction | None:
        r = self._one("transaction", id=transaction_id)
        if not r:
            return None
        r["amount"] = float(r["amount"])
        r["amount_usd"] = float(r["amount_usd"]) if r.get("amount_usd") is not None else None
        return Transaction(**r)

    def get_merchant(self, merchant_id: str) -> Merchant | None:
        r = self._one("merchant", id=merchant_id)
        return Merchant(**r) if r else None

    def get_merchants(self, merchant_ids: list[str]) -> list[Merchant]:
        if not merchant_ids:
            return []
        return [Merchant(**r) for r in self._rows("merchants", ids=list(set(merchant_ids)))]

    def get_device(self, device_id: str) -> Device | None:
        r = self._one("device", id=device_id)
        return Device(**r) if r else None

    def search(self, query: str, entity_type: str | None = None, limit: int = 20) -> list[SearchHit]:
        q = query.strip().upper()
        if not q:
            return []
        prefix = q.replace("\\", "").replace("%", "").replace("_", "") + "%"
        hits: list[SearchHit] = []
        if entity_type in (None, "customer"):
            hits += [SearchHit(entity_type="customer", entity_id=r["customer_id"],
                               label=f"{r['customer_id']} · {r['customer_type']}/{r['segment']} · {r['home_city']}")
                     for r in self._rows("search_customer", prefix=prefix, limit=limit)]
        if entity_type in (None, "account"):
            hits += [SearchHit(entity_type="account", entity_id=r["account_id"],
                               label=f"{r['account_id']} · {r['account_type']} {r['currency']} · {r['customer_id']}")
                     for r in self._rows("search_account", prefix=prefix, limit=limit)]
        if entity_type in (None, "device"):
            hits += [SearchHit(entity_type="device", entity_id=r["device_id"], label=f"{r['device_id']} · {r['device_type']}")
                     for r in self._rows("search_device", prefix=prefix, limit=limit)]
        if entity_type in (None, "merchant"):
            esc = query.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            contains = f"%{esc}%" if len(esc) >= 3 else ""
            hits += [SearchHit(entity_type="merchant", entity_id=r["merchant_id"],
                               label=f"{r['merchant_id']} · {r['name']} · {r['category']}")
                     for r in self._rows("search_merchant", prefix=prefix, contains=contains, limit=limit)]
        if entity_type in (None, "transaction") and q.startswith("TXN-"):
            hits += [SearchHit(entity_type="transaction", entity_id=r["transaction_id"],
                               label=f"{r['transaction_id']} · {r['transaction_type']} {r['amount']:.2f} {r['currency']}")
                     for r in self._rows("search_transaction", exact=q)]
        return hits[:limit]

    # ------------------------------------------------------------ transactions
    def transactions_for_accounts(self, account_ids: list[str], start: datetime | None,
                                  end: datetime | None) -> pd.DataFrame:
        if not account_ids:
            return pd.DataFrame(columns=TXN_FRAME_COLUMNS)
        df = self._txn_frame("txns_for_accounts", ids=list(account_ids), start=start or FAR_PAST, end=end or FAR_FUTURE)
        return df.sort_values("timestamp").reset_index(drop=True)

    def transactions_for_device(self, device_id: str, start: datetime | None, end: datetime | None,
                                limit: int = 500) -> pd.DataFrame:
        return self._txn_frame("txns_for_device", id=device_id, start=start or FAR_PAST, end=end or FAR_FUTURE,
                               limit=limit)

    def device_users(self, device_ids: list[str], start: datetime | None, end: datetime | None) -> pd.DataFrame:
        if not device_ids:
            return pd.DataFrame(columns=["device_id", "customer_id", "n_transactions", "first_ts", "last_ts"])
        df = self._frame("device_users", ids=list(device_ids), start=start or FAR_PAST, end=end or FAR_FUTURE)
        return _utc_frame(df, ["first_ts", "last_ts"])

    def peer_amount_stats(self, segment: str, start: datetime, end: datetime) -> dict[str, float]:
        key = (segment, str(start)[:10], str(end)[:10])
        with self._lock:
            if key in self._peer_cache:
                return self._peer_cache[key]
        r = self._one("peer_stats", segment=segment, start=start, end=end) or {}
        out = {"n": float(r.get("n") or 0)}
        if r.get("n"):
            out.update({k: float(r[k]) for k in ("p50_usd", "p95_usd", "p99_usd")})
        with self._lock:
            self._peer_cache[key] = out
        return out

    def graph_edges(self, start: datetime | None, end: datetime | None) -> dict[str, pd.DataFrame]:
        p = {"start": start or FAR_PAST, "end": end or FAR_FUTURE}
        tr = _utc_frame(self._frame("g_transfers", **p), ["first_ts", "last_ts"])
        tr["sample_txn_ids"] = tr["sample_txn_ids"].map(lambda v: list(v) if v is not None else [])
        return {
            "ownership": self._frame("g_ownership"),
            "transfers": tr,
            "device_usage": _utc_frame(self._frame("g_device_usage", **p), ["first_ts", "last_ts"]),
            "ip_usage": self._frame("g_ip_usage", **p),
            "merchant_payments": self._frame("g_merchant_payments", **p),
            "identifiers": self._frame("g_identifiers"),
            "customers": self._frame("g_customers"),
            "merchants": self._frame("g_merchants"),
            "alerts": _utc_frame(self._frame("g_alerts"), ["created_at"]),
        }

    # ------------------------------------------------------------ alerts etc.
    def list_alerts(self, entity_id: str | None = None, status: str | None = None,
                    before: datetime | None = None, limit: int = 100) -> list[Alert]:
        rows = self._rows("alerts", entity_id=entity_id or "", status=status or "", before=before or FAR_FUTURE,
                          limit=limit)
        return [Alert(**r) for r in rows]

    def alert_counts(self) -> dict[str, int]:
        return {r["severity"]: int(r["n"]) for r in self._rows("alert_counts")}

    @staticmethod
    def _inv(r: dict[str, Any]) -> Investigation:
        r = dict(r)
        r["signals"] = list(r.get("signals") or [])
        return Investigation(**r)

    def list_investigations(self, subject_ids: list[str] | None = None, status: str | None = None,
                            limit: int = 100) -> list[Investigation]:
        rows = self._rows("investigations", all_subjects=subject_ids is None, subjects=list(subject_ids or []),
                          status=status or "", limit=limit)
        return [self._inv(r) for r in rows]

    def get_investigation(self, investigation_id: str) -> Investigation | None:
        r = self._one("investigation", id=investigation_id)
        return self._inv(r) if r else None

    def create_investigation(self, inv: Investigation) -> Investigation:
        d = inv.model_dump()
        d["report"] = _j(d.get("report"))
        self._exec(SQL["insert_investigation"], **d)
        return inv

    def update_investigation(self, investigation_id: str, **fields: Any) -> Investigation | None:
        cols = [k for k in fields if k in INVESTIGATION_UPDATABLE]
        if cols:
            sets = ", ".join(f"{c} = CAST(:{c} AS jsonb)" if c in JSON_COLUMNS else f"{c} = :{c}" for c in cols)
            params = {c: (_j(fields[c]) if c in JSON_COLUMNS else fields[c]) for c in cols}
            self._exec(f"UPDATE investigations SET {sets} WHERE investigation_id = :_id", _id=investigation_id, **params)
        return self.get_investigation(investigation_id)

    def add_evidence(self, items: list[EvidenceItem]) -> None:
        from sqlalchemy import text

        with self.engine.begin() as conn:
            for it in items:
                d = it.model_dump()
                d["content"] = _j(d["content"])
                d["created_at"] = d.get("created_at") or utcnow()
                conn.execute(text(SQL["insert_evidence"]), d)

    def list_evidence(self, investigation_id: str) -> list[EvidenceItem]:
        return [EvidenceItem(**r) for r in self._rows("evidence", id=investigation_id)]

    # --------------------------------------------------------------- episodes
    def save_episode(self, episode: dict[str, Any]) -> None:
        d = {k: episode.get(k) for k in (
            "episode_id", "investigation_id", "request", "requested_by", "status", "plan", "tool_calls",
            "observations", "retrieved_evidence", "reasoning_summary", "final_output", "evaluation",
            "human_feedback", "model", "tokens_in", "tokens_out", "latency_ms", "started_at", "finished_at")}
        for k in ("plan", "tool_calls", "observations", "retrieved_evidence"):
            d[k] = _j(d[k] or [])
        for k in ("final_output", "evaluation", "human_feedback"):
            d[k] = _j(d[k])
        d["tokens_in"] = d["tokens_in"] or 0
        d["tokens_out"] = d["tokens_out"] or 0
        d["started_at"] = d["started_at"] or utcnow()
        self._exec(SQL["insert_episode"], **d)

    def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        return self._one("episode", id=episode_id)

    def episodes_for_investigation(self, investigation_id: str) -> list[dict[str, Any]]:
        return self._rows("episodes_for_investigation", id=investigation_id)

    def list_episodes(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._rows("episodes", limit=limit)

    def update_episode(self, episode_id: str, **fields: Any) -> None:
        cols = [k for k in fields if k in EPISODE_UPDATABLE]
        if not cols:
            return
        sets = ", ".join(f"{c} = CAST(:{c} AS jsonb)" if c in JSON_COLUMNS else f"{c} = :{c}" for c in cols)
        params = {c: (_j(fields[c]) if c in JSON_COLUMNS else fields[c]) for c in cols}
        self._exec(f"UPDATE agent_episodes SET {sets} WHERE episode_id = :_id", _id=episode_id, **params)

    def add_decision(self, decision: HumanDecision) -> None:
        self._exec(SQL["insert_decision"], **decision.model_dump())

    def list_decisions(self, investigation_id: str | None = None, limit: int = 500) -> list[HumanDecision]:
        rows = self._rows("decisions", all=investigation_id is None, id=investigation_id or "", limit=limit)
        return [HumanDecision(**{**r, "failure_categories": list(r.get("failure_categories") or [])}) for r in rows]

    def append_audit(self, event: AuditEvent) -> None:
        d = event.model_dump(exclude={"id"})
        d["details"] = _j(d.get("details") or {})
        self._exec(SQL["insert_audit"], **d)

    def list_audit(self, limit: int = 200, user_id: str | None = None, action: str | None = None) -> list[AuditEvent]:
        return [AuditEvent(**r) for r in self._rows("audit", user_id=user_id or "", action=action or "", limit=limit)]

    def save_config_version(self, record: dict[str, Any]) -> None:
        d = {k: record.get(k) for k in ("version_id", "created_at", "created_by", "status", "rationale", "config",
                                        "evaluation", "approved_by", "approved_at")}
        d["config"], d["evaluation"] = _j(d["config"]), _j(d["evaluation"])
        d["created_at"] = d["created_at"] or utcnow()
        self._exec(SQL["insert_config"], **d)

    def list_config_versions(self) -> list[dict[str, Any]]:
        return self._rows("configs")

    def update_config_version(self, version_id: str, **fields: Any) -> None:
        cols = [k for k in fields if k in CONFIG_UPDATABLE]
        if not cols:
            return
        sets = ", ".join(f"{c} = CAST(:{c} AS jsonb)" if c in JSON_COLUMNS else f"{c} = :{c}" for c in cols)
        params = {c: (_j(fields[c]) if c in JSON_COLUMNS else fields[c]) for c in cols}
        self._exec(f"UPDATE risk_config_versions SET {sets} WHERE version_id = :_id", _id=version_id, **params)

    def save_evaluation_run(self, record: dict[str, Any]) -> None:
        self._exec(SQL["insert_eval"], run_id=record["run_id"], created_at=record.get("created_at") or utcnow(),
                   kind=record["kind"], config_version=record.get("config_version"), metrics=_j(record["metrics"]),
                   details=_j(record.get("details") or {}))

    def list_evaluation_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._rows("evals", limit=limit)

    def get_user(self, username: str) -> dict[str, Any] | None:
        return self._one("user", username=username)

    def upsert_user(self, user: dict[str, Any]) -> None:
        self._exec(SQL["upsert_user"], user_id=user["user_id"], username=user["username"],
                   password_hash=user["password_hash"], role=user["role"], active=user.get("active", True))

    def dashboard_stats(self) -> dict[str, Any]:
        counts = self._one("counts") or {}
        by_status = {r["status"]: int(r["n"]) for r in self._rows("inv_by_status")}
        hist = {int(r["b"]): int(r["n"]) for r in self._rows("risk_hist")}
        buckets = ["0-20", "20-40", "40-60", "60-80", "80-100"]
        dist = [{"bucket": b, "count": hist.get(i + 1, 0) + (hist.get(6, 0) if i == 4 else 0)}
                for i, b in enumerate(buckets)]
        return {"counts": {k: int(v) for k, v in counts.items()}, "open_alerts_by_severity": self.alert_counts(),
                "investigations_by_status": by_status, "risk_distribution": dist, "as_of": self.as_of().isoformat(),
                "generated_at": utcnow().isoformat()}
