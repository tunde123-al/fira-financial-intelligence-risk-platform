"""Graph intelligence contract and result models.

The financial graph is a deliberate *projection*, not a copy of the relational
schema. Transactions stay in PostgreSQL; the graph keeps aggregated relationships
that are expensive to answer relationally:

    (:Customer)-[:OWNS]->(:Account)
    (:Account)-[:TRANSFERRED_TO {n, total_usd, first_ts, last_ts, sample_txn_ids}]->(:Account)
    (:Account)-[:PAID {n, total_usd}]->(:Merchant)
    (:Customer)-[:USES_DEVICE {n, first_ts, last_ts}]->(:Device)
    (:Customer)-[:USES_IP {n}]->(:IPAddress)
    (:Customer)-[:HAS_IDENTIFIER]->(:Phone|:Email|:Address)   (hashed values only)

Every node id is "<type>:<natural id>", e.g. "account:ACC-200001".
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field


def node_id(kind: str, natural_id: str) -> str:
    return f"{kind}:{natural_id}"


def split_node_id(nid: str) -> tuple[str, str]:
    kind, _, rest = nid.partition(":")
    return kind, rest


class GraphNode(BaseModel):
    id: str
    kind: str
    label: str
    props: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    source: str
    target: str
    rel: str
    props: dict[str, Any] = Field(default_factory=dict)


class Subgraph(BaseModel):
    center: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool = False


class SharedDevice(BaseModel):
    device_id: str
    customers: list[str]
    n_customers: int


class PathResult(BaseModel):
    found: bool
    nodes: list[str] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    hops: int = 0


class ConnectedAccount(BaseModel):
    account_id: str
    owner_customer_id: str | None
    hops: int
    via: str


class FundFlow(BaseModel):
    path: list[str]
    hops: int
    total_usd_first_hop: float
    min_edge_usd: float
    first_ts: str | None = None
    last_ts: str | None = None
    sample_txn_ids: list[str] = Field(default_factory=list)


class Cluster(BaseModel):
    center: str
    members: list[str]
    customers: list[str]
    accounts: list[str]
    devices: list[str]
    density: float
    flagged_customers: list[str]
    central_entities: list[dict[str, Any]]
    shared_devices: list[SharedDevice]


class GraphBackend(Protocol):
    name: str

    def related_entities(self, kind: str, natural_id: str, depth: int = 2, limit: int = 150) -> Subgraph: ...
    def shared_devices(self, customer_id: str, min_customers: int = 2) -> list[SharedDevice]: ...
    def transaction_path(self, source_account: str, target_account: str, max_hops: int = 6) -> PathResult: ...
    def connected_accounts(self, account_id: str, max_hops: int = 2, limit: int = 100) -> list[ConnectedAccount]: ...
    def suspicious_cluster(self, customer_id: str, max_hops: int = 2) -> Cluster: ...
    def trace_funds(self, account_id: str, direction: str = "out", max_hops: int = 3,
                    since: datetime | None = None, limit: int = 50) -> list[FundFlow]: ...
    def candidate_cycles(self, account_ids: list[str], max_len: int = 5,
                         since: datetime | None = None) -> list[list[str]]: ...
    def counterparties(self, account_ids: list[str], since: datetime | None = None) -> list[str]: ...
    def account_owner(self, account_id: str) -> str | None: ...
    def customer_flags(self, customer_ids: list[str]) -> dict[str, bool]: ...
    def stats(self) -> dict[str, Any]: ...
    def ping(self) -> bool: ...
