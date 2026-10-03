"""Investigator tools. Each returns a typed Pydantic object (never free text)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from app.analytics.geo import detect_geographic_outliers, find_transactions_near_location
from app.analytics.stats import period_stats, split_customer_frame
from app.data.store import new_id, utcnow
from app.documents.models import Passage
from app.graph.base import Cluster, ConnectedAccount, FundFlow, PathResult, SharedDevice, Subgraph
from app.risk.models import RiskAssessment
from app.schemas.domain import (
    ID_PATTERNS,
    Account,
    Customer,
    CustomerIdentifier,
    EvidenceItem,
    HumanDecision,
    Investigation,
    Transaction,
)
from app.tools.registry import ToolContext, ToolRegistry, ToolSpec

CUST = Field(pattern=ID_PATTERNS["customer"])
ACC = Field(pattern=ID_PATTERNS["account"])


# ----------------------------------------------------------------- schemas
class CustomerIn(BaseModel):
    customer_id: str = CUST


class CustomerOut(BaseModel):
    customer: Customer
    identifiers: list[CustomerIdentifier]
    accounts: list[Account]
    shared_identifier_customers: list[dict[str, Any]]


class AccountIn(BaseModel):
    account_id: str = ACC


class AccountOut(BaseModel):
    account: Account
    owner: Customer | None


class TransactionLookupIn(BaseModel):
    transaction_id: str = Field(pattern=ID_PATTERNS["transaction"])


class TransactionsIn(BaseModel):
    customer_id: str | None = Field(default=None, pattern=ID_PATTERNS["customer"])
    account_ids: list[str] = Field(default_factory=list, max_length=50)
    start: datetime | None = None
    end: datetime | None = None
    lookback_days: int | None = Field(default=None, ge=1, le=730)
    limit: int = Field(default=200, ge=1, le=2000)


class TransactionsOut(BaseModel):
    transactions: list[Transaction]
    total: int
    truncated: bool
    start: datetime | None
    end: datetime | None


class StatsIn(BaseModel):
    customer_id: str = CUST
    lookback_days: int = Field(default=30, ge=1, le=365)
    baseline_days: int = Field(default=90, ge=7, le=730)


class StatsOut(BaseModel):
    customer_id: str
    window: dict[str, Any]
    baseline: dict[str, Any]


class BaselineIn(BaseModel):
    customer_id: str = CUST
    baseline_days: int = Field(default=90, ge=7, le=730)
    end: datetime | None = None


class BaselineOut(BaseModel):
    customer_id: str
    baseline: dict[str, Any]


class RiskIn(BaseModel):
    entity_type: Literal["customer", "account"] = "customer"
    entity_id: str = Field(pattern=r"^(CUST|ACC)-\d{1,10}$")
    lookback_days: int = Field(default=30, ge=1, le=365)
    baseline_days: int = Field(default=90, ge=7, le=730)
    window_end: datetime | None = None


class AnomalyIn(BaseModel):
    customer_id: str = CUST
    lookback_days: int = Field(default=30, ge=1, le=365)
    baseline_days: int = Field(default=90, ge=7, le=730)
    z_threshold: float = Field(default=6.0, ge=2, le=50)


class TxnAnomaly(BaseModel):
    transaction_id: str
    kind: Literal["amount_outlier", "geographic_outlier"]
    observed: float
    reference: float
    detail: str


class AnomalyOut(BaseModel):
    customer_id: str
    anomalies: list[TxnAnomaly]
    method: str


class EntityIn(BaseModel):
    kind: Literal["customer", "account", "device", "merchant"]
    entity_id: str = Field(pattern=r"^(CUST|ACC|DEV|MER)-\d{1,10}$")
    depth: int = Field(default=2, ge=1, le=3)
    limit: int = Field(default=150, ge=10, le=500)


class SharedDeviceOut(BaseModel):
    customer_id: str
    shared_devices: list[SharedDevice]


class PathIn(BaseModel):
    source_account: str = ACC
    target_account: str = ACC
    max_hops: int = Field(default=6, ge=1, le=8)


class TraceIn(BaseModel):
    account_id: str = ACC
    direction: Literal["in", "out"] = "out"
    max_hops: int = Field(default=3, ge=1, le=5)
    since: datetime | None = None
    limit: int = Field(default=20, ge=1, le=100)


class TraceOut(BaseModel):
    account_id: str
    direction: str
    flows: list[FundFlow]


class ConnectedIn(BaseModel):
    account_id: str = ACC
    max_hops: int = Field(default=2, ge=1, le=4)
    limit: int = Field(default=50, ge=1, le=200)


class ConnectedOut(BaseModel):
    account_id: str
    connected: list[ConnectedAccount]


class ClusterIn(BaseModel):
    customer_id: str = CUST
    max_hops: int = Field(default=2, ge=1, le=3)


class GraphSearchIn(BaseModel):
    kind: Literal["customer", "account", "device", "merchant"]
    entity_id: str = Field(pattern=r"^(CUST|ACC|DEV|MER)-\d{1,10}$")


class GraphSearchOut(BaseModel):
    found: bool
    node: dict[str, Any] | None
    degree_by_relation: dict[str, int]
    backend: str


class DocSearchIn(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    k: int = Field(default=5, ge=1, le=20)
    doc_types: list[str] | None = None
    mode: Literal["hybrid", "semantic", "keyword"] = "hybrid"


class DocSearchOut(BaseModel):
    query: str
    passages: list[Passage]
    semantic_available: bool


class PolicyIn(BaseModel):
    document_id: str | None = Field(default=None, max_length=80)
    topic: str | None = Field(default=None, max_length=300)
    k: int = Field(default=5, ge=1, le=30)


class HistoryIn(BaseModel):
    subject_ids: list[str] = Field(min_length=1, max_length=200)
    limit: int = Field(default=20, ge=1, le=200)


class HistoryItem(BaseModel):
    investigation: Investigation
    decisions: list[HumanDecision]


class HistoryOut(BaseModel):
    items: list[HistoryItem]


class CreateInvestigationIn(BaseModel):
    subject_id: str = Field(pattern=r"^(CUST|ACC|TXN|DEV|MER)-\d{1,12}$")
    subject_type: Literal["customer", "account", "transaction", "device", "merchant"]
    request_text: str | None = Field(default=None, max_length=2000)
    assigned_to: str | None = Field(default=None, max_length=80)


class AddEvidenceIn(BaseModel):
    investigation_id: str = Field(pattern=ID_PATTERNS["investigation"])
    items: list[EvidenceItem] = Field(max_length=500)


class AddEvidenceOut(BaseModel):
    investigation_id: str
    added: int


class ReportIn(BaseModel):
    investigation_id: str = Field(pattern=ID_PATTERNS["investigation"])


class ReportOut(BaseModel):
    investigation_id: str
    status: str
    report: dict[str, Any] | None


class NearIn(BaseModel):
    customer_id: str = CUST
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    radius_km: float = Field(default=50, gt=0, le=2000)
    lookback_days: int = Field(default=90, ge=1, le=730)


class NearOut(BaseModel):
    customer_id: str
    transactions: list[dict[str, Any]]


# --------------------------------------------------------------- helpers
def _window(ctx: ToolContext, lookback_days: int | None, start: datetime | None, end: datetime | None):
    end = end or ctx.services.store.as_of()
    if start is None and lookback_days:
        start = end - timedelta(days=lookback_days)
    return start, end


def _txns(df: pd.DataFrame) -> list[Transaction]:
    recs = df.replace({np.nan: None}).to_dict("records")
    out = []
    for r in recs:
        r["timestamp"] = pd.Timestamp(r["timestamp"]).to_pydatetime()
        out.append(Transaction(**r))
    return out


def _require(obj: Any, what: str) -> Any:
    if obj is None:
        raise LookupError(f"{what} not found")
    return obj


# ----------------------------------------------------------------- tools
def get_customer(ctx: ToolContext, i: CustomerIn) -> CustomerOut:
    s = ctx.services.store
    c = _require(s.get_customer(i.customer_id), f"customer {i.customer_id}")
    idents = [CustomerIdentifier(identifier_type=x.identifier_type, masked_value=x.masked_value)
              for x in s.get_identifiers(i.customer_id)]
    shared = s.customers_sharing_identifiers(i.customer_id)
    return CustomerOut(customer=c, identifiers=idents, accounts=s.accounts_for_customer(i.customer_id),
                       shared_identifier_customers=shared[:50])


def get_account(ctx: ToolContext, i: AccountIn) -> AccountOut:
    s = ctx.services.store
    a = _require(s.get_account(i.account_id), f"account {i.account_id}")
    return AccountOut(account=a, owner=s.get_customer(a.customer_id))


def get_transaction(ctx: ToolContext, i: TransactionLookupIn) -> Transaction:
    return _require(ctx.services.store.get_transaction(i.transaction_id), f"transaction {i.transaction_id}")


def get_transactions(ctx: ToolContext, i: TransactionsIn) -> TransactionsOut:
    s = ctx.services.store
    accs = list(i.account_ids)
    if i.customer_id:
        _require(s.get_customer(i.customer_id), f"customer {i.customer_id}")
        accs += [a.account_id for a in s.accounts_for_customer(i.customer_id)]
    if not accs:
        raise LookupError("no accounts specified")
    start, end = _window(ctx, i.lookback_days, i.start, i.end)
    df = s.transactions_for_accounts(sorted(set(accs)), start, end)
    total = len(df)
    df = df.sort_values("timestamp", ascending=False).head(i.limit)
    return TransactionsOut(transactions=_txns(df), total=total, truncated=total > i.limit, start=start, end=end)


def get_transaction_statistics(ctx: ToolContext, i: StatsIn) -> StatsOut:
    s = ctx.services.store
    _require(s.get_customer(i.customer_id), f"customer {i.customer_id}")
    accs = {a.account_id for a in s.accounts_for_customer(i.customer_id)}
    end = s.as_of()
    ws = end - timedelta(days=i.lookback_days)
    bs = ws - timedelta(days=i.baseline_days)
    tx = s.transactions_for_accounts(sorted(accs), bs, end)
    base = tx[tx.timestamp < pd.Timestamp(ws)]
    win = tx[tx.timestamp >= pd.Timestamp(ws)]
    return StatsOut(customer_id=i.customer_id, window=period_stats(split_customer_frame(win, accs), ws, end),
                    baseline=period_stats(split_customer_frame(base, accs), bs, ws))


def calculate_customer_baseline(ctx: ToolContext, i: BaselineIn) -> BaselineOut:
    s = ctx.services.store
    _require(s.get_customer(i.customer_id), f"customer {i.customer_id}")
    accs = {a.account_id for a in s.accounts_for_customer(i.customer_id)}
    end = i.end or s.as_of()
    start = end - timedelta(days=i.baseline_days)
    tx = s.transactions_for_accounts(sorted(accs), start, end)
    return BaselineOut(customer_id=i.customer_id, baseline=period_stats(split_customer_frame(tx, accs), start, end))


def get_risk_signals(ctx: ToolContext, i: RiskIn) -> RiskAssessment:
    eng = ctx.services.risk_engine
    if i.entity_type == "account":
        return eng.assess_account(i.entity_id, i.window_end, i.lookback_days, i.baseline_days)
    return eng.assess_customer(i.entity_id, i.window_end, i.lookback_days, i.baseline_days)


def detect_anomalies(ctx: ToolContext, i: AnomalyIn) -> AnomalyOut:
    """Transaction-level anomalies: robust z-score of amount vs the customer's own
    baseline, and distance-from-home outliers vs the baseline distance profile."""
    s = ctx.services.store
    c = _require(s.get_customer(i.customer_id), f"customer {i.customer_id}")
    accs = {a.account_id for a in s.accounts_for_customer(i.customer_id)}
    end = s.as_of()
    ws = end - timedelta(days=i.lookback_days)
    bs = ws - timedelta(days=i.baseline_days)
    tx = s.transactions_for_accounts(sorted(accs), bs, end)
    base = split_customer_frame(tx[tx.timestamp < pd.Timestamp(ws)], accs).outbound
    win = split_customer_frame(tx[tx.timestamp >= pd.Timestamp(ws)], accs).outbound
    out: list[TxnAnomaly] = []
    b = base[base.status == "completed"].amount_usd
    if len(b) >= 5:
        med = float(b.median())
        mad = float((b - med).abs().median()) * 1.4826 or max(med * 0.1, 1.0)
        for r in win[win.status == "completed"].itertuples():
            z = (float(r.amount_usd) - med) / mad
            if z >= i.z_threshold:
                out.append(TxnAnomaly(transaction_id=r.transaction_id, kind="amount_outlier",
                                      observed=round(float(r.amount_usd), 2), reference=round(med, 2),
                                      detail=f"robust z={z:.1f} vs baseline median USD {med:,.2f}"))
    geo = detect_geographic_outliers(win, c.home_latitude, c.home_longitude, base)
    for r in geo.itertuples():
        out.append(TxnAnomaly(transaction_id=r.transaction_id, kind="geographic_outlier",
                              observed=float(r.distance_from_home_km), reference=0.0,
                              detail=f"{r.distance_from_home_km:,.0f} km from home ({r.country})"))
    return AnomalyOut(customer_id=i.customer_id, anomalies=out[:100],
                      method="robust z-score (median/MAD) on amount_usd; haversine distance vs baseline p99")


def get_related_entities(ctx: ToolContext, i: EntityIn) -> Subgraph:
    return ctx.services.graph.related_entities(i.kind, i.entity_id, i.depth, i.limit)


def find_shared_device(ctx: ToolContext, i: CustomerIn) -> SharedDeviceOut:
    return SharedDeviceOut(customer_id=i.customer_id, shared_devices=ctx.services.graph.shared_devices(i.customer_id))


def trace_transaction_path(ctx: ToolContext, i: PathIn) -> PathResult:
    return ctx.services.graph.transaction_path(i.source_account, i.target_account, i.max_hops)


def trace_funds(ctx: ToolContext, i: TraceIn) -> TraceOut:
    flows = ctx.services.graph.trace_funds(i.account_id, i.direction, i.max_hops, i.since, i.limit)
    return TraceOut(account_id=i.account_id, direction=i.direction, flows=flows)


def find_connected_accounts(ctx: ToolContext, i: ConnectedIn) -> ConnectedOut:
    return ConnectedOut(account_id=i.account_id,
                        connected=ctx.services.graph.connected_accounts(i.account_id, i.max_hops, i.limit))


def find_suspicious_cluster(ctx: ToolContext, i: ClusterIn) -> Cluster:
    return ctx.services.graph.suspicious_cluster(i.customer_id, i.max_hops)


def search_graph(ctx: ToolContext, i: GraphSearchIn) -> GraphSearchOut:
    sg = ctx.services.graph.related_entities(i.kind, i.entity_id, depth=1, limit=500)
    if not sg.nodes:
        return GraphSearchOut(found=False, node=None, degree_by_relation={}, backend=ctx.services.graph.name)
    center = next(n for n in sg.nodes if n.id == sg.center)
    deg: dict[str, int] = {}
    for e in sg.edges:
        if sg.center in (e.source, e.target):
            deg[e.rel] = deg.get(e.rel, 0) + 1
    return GraphSearchOut(found=True, node=center.model_dump(), degree_by_relation=deg,
                          backend=ctx.services.graph.name)


POLICY_TYPES = ["aml_policy", "kyc_policy", "internal_procedure", "typology_guidance", "regulatory_document",
                "internal_memo"]


def search_documents(ctx: ToolContext, i: DocSearchIn) -> DocSearchOut:
    r = ctx.services.retriever
    return DocSearchOut(query=i.query, passages=r.search(i.query, i.k, i.doc_types, mode=i.mode),
                        semantic_available=r.semantic_available)


def get_policy(ctx: ToolContext, i: PolicyIn) -> DocSearchOut:
    r = ctx.services.retriever
    if i.document_id:
        chunks = [c for c in r.repo.all_chunks() if c.document_id == i.document_id][: i.k]
        if not chunks:
            raise LookupError(f"document {i.document_id} not found")
        meta = r.repo.chunks([c.chunk_id for c in chunks])
        passages = [Passage(chunk_id=c.chunk_id, document_id=c.document_id, title=meta[c.chunk_id].get("title") or "",
                            doc_type=meta[c.chunk_id].get("doc_type") or "", page=c.page, section=c.section,
                            source=meta[c.chunk_id].get("source") or "", text=c.text, score=1.0) for c in chunks]
        return DocSearchOut(query=i.document_id, passages=passages, semantic_available=r.semantic_available)
    if not i.topic:
        raise LookupError("either document_id or topic is required")
    return DocSearchOut(query=i.topic, passages=r.search(i.topic, i.k, POLICY_TYPES),
                        semantic_available=r.semantic_available)


def get_previous_investigations(ctx: ToolContext, i: HistoryIn) -> HistoryOut:
    s = ctx.services.store
    invs = [x for x in s.list_investigations(subject_ids=i.subject_ids, limit=i.limit * 3)
            if x.investigation_id != ctx.investigation_id][: i.limit]
    return HistoryOut(items=[HistoryItem(investigation=x.model_copy(update={"report": None}),
                                         decisions=s.list_decisions(x.investigation_id)) for x in invs])


def create_investigation(ctx: ToolContext, i: CreateInvestigationIn) -> Investigation:
    inv = Investigation(investigation_id=new_id("INV"), subject_id=i.subject_id, subject_type=i.subject_type,
                        assigned_to=i.assigned_to or ctx.principal.user_id, status="open", created_at=utcnow(),
                        request_text=i.request_text, created_by=ctx.principal.user_id)
    return ctx.services.store.create_investigation(inv)


def add_evidence(ctx: ToolContext, i: AddEvidenceIn) -> AddEvidenceOut:
    s = ctx.services.store
    _require(s.get_investigation(i.investigation_id), f"investigation {i.investigation_id}")
    items = [it.model_copy(update={"investigation_id": i.investigation_id}) for it in i.items]
    s.add_evidence(items)
    return AddEvidenceOut(investigation_id=i.investigation_id, added=len(items))


def generate_report(ctx: ToolContext, i: ReportIn) -> ReportOut:
    inv = _require(ctx.services.store.get_investigation(i.investigation_id), f"investigation {i.investigation_id}")
    return ReportOut(investigation_id=inv.investigation_id, status=inv.status, report=inv.report)


def find_transactions_near_location_tool(ctx: ToolContext, i: NearIn) -> NearOut:
    s = ctx.services.store
    _require(s.get_customer(i.customer_id), f"customer {i.customer_id}")
    accs = [a.account_id for a in s.accounts_for_customer(i.customer_id)]
    end = s.as_of()
    tx = s.transactions_for_accounts(accs, end - timedelta(days=i.lookback_days), end)
    near = find_transactions_near_location(tx, i.latitude, i.longitude, i.radius_km).head(200)
    cols = ["transaction_id", "timestamp", "amount_usd", "transaction_type", "channel", "country", "distance_km"]
    recs = near[cols].replace({np.nan: None}).to_dict("records")
    for r in recs:
        r["timestamp"] = pd.Timestamp(r["timestamp"]).isoformat()
        r["distance_km"] = round(float(r["distance_km"]), 1)
    return NearOut(customer_id=i.customer_id, transactions=recs)


def _ent(kind: str, attr: str):
    return lambda inp: (kind, getattr(inp, attr, None))


def build_registry(default_timeout_s: float = 20.0) -> ToolRegistry:
    reg = ToolRegistry(default_timeout_s=default_timeout_s)
    T = ToolSpec
    for spec in [
        T("get_customer", "Customer profile, masked identifiers, accounts and customers sharing identifiers.",
          CustomerIn, CustomerOut, get_customer, audit_entity=_ent("customer", "customer_id")),
        T("get_account", "Account record and its owner.", AccountIn, AccountOut, get_account,
          audit_entity=_ent("account", "account_id")),
        T("get_transaction", "One transaction by id.", TransactionLookupIn, Transaction, get_transaction,
          audit_entity=_ent("transaction", "transaction_id")),
        T("get_transactions", "Transactions for a customer or accounts in a time window (most recent first).",
          TransactionsIn, TransactionsOut, get_transactions, audit_entity=_ent("customer", "customer_id")),
        T("get_transaction_statistics", "Behavioural statistics for the window and the preceding baseline.",
          StatsIn, StatsOut, get_transaction_statistics, audit_entity=_ent("customer", "customer_id")),
        T("calculate_customer_baseline", "Baseline behavioural statistics for a customer.", BaselineIn, BaselineOut,
          calculate_customer_baseline, audit_entity=_ent("customer", "customer_id")),
        T("get_risk_signals", "Deterministic risk assessment: signals, contributions and score.", RiskIn,
          RiskAssessment, get_risk_signals, timeout_s=60, audit_entity=lambda i: (i.entity_type, i.entity_id)),
        T("detect_anomalies", "Transaction-level statistical and geographic outliers.", AnomalyIn, AnomalyOut,
          detect_anomalies, audit_entity=_ent("customer", "customer_id")),
        T("get_related_entities", "Bounded graph neighbourhood of an entity.", EntityIn, Subgraph,
          get_related_entities, audit_entity=lambda i: (i.kind, i.entity_id)),
        T("find_shared_device", "Devices this customer shares with other customers.", CustomerIn, SharedDeviceOut,
          find_shared_device, audit_entity=_ent("customer", "customer_id")),
        T("trace_transaction_path", "Shortest transfer path between two accounts.", PathIn, PathResult,
          trace_transaction_path, audit_entity=_ent("account", "source_account")),
        T("trace_funds", "Follow transfers out of (or into) an account for several hops.", TraceIn, TraceOut,
          trace_funds, audit_entity=_ent("account", "account_id")),
        T("find_connected_accounts", "Accounts connected by ownership, shared devices or transfers.", ConnectedIn,
          ConnectedOut, find_connected_accounts, audit_entity=_ent("account", "account_id")),
        T("find_suspicious_cluster", "Cluster around a customer with density, centrality and flagged members.",
          ClusterIn, Cluster, find_suspicious_cluster, audit_entity=_ent("customer", "customer_id")),
        T("search_graph", "Look up an entity node and its relationship degrees.", GraphSearchIn, GraphSearchOut,
          search_graph, audit_entity=lambda i: (i.kind, i.entity_id)),
        T("search_documents", "Hybrid semantic+keyword search over policies and documents with provenance.",
          DocSearchIn, DocSearchOut, search_documents),
        T("get_policy", "Passages of a policy document by id, or policy passages for a topic.", PolicyIn,
          DocSearchOut, get_policy),
        T("get_previous_investigations", "Previous investigations and human decisions for subjects.", HistoryIn,
          HistoryOut, get_previous_investigations),
        T("create_investigation", "Open a new investigation record.", CreateInvestigationIn, Investigation,
          create_investigation, side_effects=True, audit_entity=lambda i: (i.subject_type, i.subject_id)),
        T("add_evidence", "Attach evidence items to an investigation.", AddEvidenceIn, AddEvidenceOut, add_evidence,
          side_effects=True, audit_entity=lambda i: ("investigation", i.investigation_id)),
        T("generate_report", "Return the stored investigation report.", ReportIn, ReportOut, generate_report,
          audit_entity=lambda i: ("investigation", i.investigation_id)),
        T("find_transactions_near_location", "Customer transactions within a radius of a point.", NearIn, NearOut,
          find_transactions_near_location_tool, audit_entity=_ent("customer", "customer_id")),
    ]:
        reg.register(spec)
    return reg
