"""In-process graph backend (NetworkX).

Builds the projection described in `app.graph.base` from `DataStore.graph_edges`.
Used for local development without Neo4j, for the offline evaluation harness and
in tests. Answers the same questions as `Neo4jGraph` with the same result models.
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from typing import Any

import networkx as nx
import pandas as pd

from app.graph.base import (
    Cluster,
    ConnectedAccount,
    FundFlow,
    GraphEdge,
    GraphNode,
    PathResult,
    SharedDevice,
    Subgraph,
    node_id,
    split_node_id,
)

# Nodes that connect thousands of unrelated customers are not traversed through.
HUB_KINDS = {"merchant", "ip"}
HUB_DEGREE_LIMIT = 60


def _epoch(x: Any) -> float | None:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.timestamp()


def _iso(x: Any) -> str | None:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    return pd.Timestamp(x).isoformat()


def build_projection(edges: dict[str, pd.DataFrame]) -> nx.DiGraph:
    g = nx.DiGraph()
    flagged: set[str] = set()
    al = edges.get("alerts")
    if al is not None and not al.empty:
        flagged = set(al[(al.status == "open") & (al.entity_type == "customer")].entity_id)
    for r in edges["customers"].itertuples(index=False):
        g.add_node(node_id("customer", r.customer_id), kind="customer", label=r.customer_id,
                   segment=r.segment, customer_type=r.customer_type, risk_profile=r.risk_profile,
                   country=r.country, open_alert=r.customer_id in flagged)
    for r in edges["merchants"].itertuples(index=False):
        g.add_node(node_id("merchant", r.merchant_id), kind="merchant", label=f"{r.name} ({r.category})",
                   category=r.category, risk_profile=r.risk_profile, country=r.country)
    for r in edges["ownership"].itertuples(index=False):
        a = node_id("account", r.account_id)
        g.add_node(a, kind="account", label=r.account_id)
        g.add_edge(node_id("customer", r.customer_id), a, rel="OWNS")
    for r in edges["transfers"].itertuples(index=False):
        g.add_edge(node_id("account", r.sender_account_id), node_id("account", r.receiver_account_id),
                   rel="TRANSFERRED_TO", n=int(r.n), total_usd=round(float(r.total_usd), 2),
                   first_ts=_iso(r.first_ts), last_ts=_iso(r.last_ts),
                   first_epoch=_epoch(r.first_ts), last_epoch=_epoch(r.last_ts),
                   sample_txn_ids=list(r.sample_txn_ids) if isinstance(r.sample_txn_ids, (list, tuple)) else [])
    for r in edges["device_usage"].itertuples(index=False):
        d = node_id("device", r.device_id)
        if d not in g:
            g.add_node(d, kind="device", label=r.device_id)
        g.add_edge(node_id("customer", r.customer_id), d, rel="USES_DEVICE", n=int(r.n),
                   first_ts=_iso(r.first_ts), last_ts=_iso(r.last_ts),
                   first_epoch=_epoch(r.first_ts), last_epoch=_epoch(r.last_ts))
    for r in edges["ip_usage"].itertuples(index=False):
        i = node_id("ip", r.ip_address)
        if i not in g:
            g.add_node(i, kind="ip", label=r.ip_address)
        g.add_edge(node_id("customer", r.customer_id), i, rel="USES_IP", n=int(r.n))
    for r in edges["merchant_payments"].itertuples(index=False):
        g.add_edge(node_id("account", r.account_id), node_id("merchant", r.merchant_id), rel="PAID",
                   n=int(r.n), total_usd=round(float(r.total_usd), 2))
    for r in edges["identifiers"].itertuples(index=False):
        i = node_id(r.identifier_type, r.value_hash[:16])
        if i not in g:
            g.add_node(i, kind=r.identifier_type, label=r.masked_value)
        g.add_edge(node_id("customer", r.customer_id), i, rel="HAS_IDENTIFIER")
    return g


class NetworkXGraph:
    name = "networkx"

    def __init__(self, graph: nx.DiGraph):
        self.g = graph
        self._lock = threading.RLock()

    @classmethod
    def from_store(cls, store: Any, start: datetime | None = None, end: datetime | None = None) -> NetworkXGraph:
        return cls(build_projection(store.graph_edges(start, end)))

    # ------------------------------------------------------------------ helpers
    def _is_hub(self, n: str) -> bool:
        kind = self.g.nodes[n].get("kind")
        return kind in HUB_KINDS or (self.g.degree(n) > HUB_DEGREE_LIMIT and kind != "customer")

    def _node(self, n: str) -> GraphNode:
        d = dict(self.g.nodes[n])
        kind = d.pop("kind", split_node_id(n)[0])
        label = d.pop("label", n)
        return GraphNode(id=n, kind=kind, label=str(label), props={k: v for k, v in d.items() if v is not None})

    def _edge(self, u: str, v: str) -> GraphEdge:
        d = dict(self.g.edges[u, v])
        rel = d.pop("rel")
        d.pop("first_epoch", None)
        d.pop("last_epoch", None)
        return GraphEdge(source=u, target=v, rel=rel, props=d)

    def _transfer_ok(self, u: str, v: str, since: float | None) -> bool:
        d = self.g.edges[u, v]
        if d.get("rel") != "TRANSFERRED_TO":
            return False
        return since is None or (d.get("last_epoch") or 0) >= since

    # ------------------------------------------------------------------ queries
    def ping(self) -> bool:
        return True

    def stats(self) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        for _, k in self.g.nodes(data="kind"):
            kinds[k] = kinds.get(k, 0) + 1
        rels: dict[str, int] = {}
        for _, _, r in self.g.edges(data="rel"):
            rels[r] = rels.get(r, 0) + 1
        return {"backend": self.name, "nodes": self.g.number_of_nodes(), "edges": self.g.number_of_edges(),
                "node_kinds": kinds, "edge_types": rels}

    def related_entities(self, kind: str, natural_id: str, depth: int = 2, limit: int = 150) -> Subgraph:
        center = node_id(kind, natural_id)
        if center not in self.g:
            return Subgraph(center=center, nodes=[], edges=[])
        seen = {center: 0}
        q = deque([center])
        truncated = False
        while q:
            n = q.popleft()
            if seen[n] >= depth or (n != center and self._is_hub(n)):
                continue
            for m in list(self.g.successors(n)) + list(self.g.predecessors(n)):
                if m in seen:
                    continue
                if len(seen) >= limit:
                    truncated = True
                    break
                seen[m] = seen[n] + 1
                q.append(m)
        sub = self.g.subgraph(seen)
        nodes = [self._node(n).model_copy(update={"props": {**self._node(n).props, "hops": seen[n]}})
                 for n in sub.nodes]
        edges = [self._edge(u, v) for u, v in sub.edges]
        return Subgraph(center=center, nodes=nodes, edges=edges, truncated=truncated)

    def shared_devices(self, customer_id: str, min_customers: int = 2) -> list[SharedDevice]:
        c = node_id("customer", customer_id)
        if c not in self.g:
            return []
        out = []
        for d in self.g.successors(c):
            if self.g.nodes[d].get("kind") != "device":
                continue
            users = sorted(split_node_id(u)[1] for u in self.g.predecessors(d)
                           if self.g.nodes[u].get("kind") == "customer")
            if len(users) >= min_customers:
                out.append(SharedDevice(device_id=split_node_id(d)[1], customers=users, n_customers=len(users)))
        return sorted(out, key=lambda s: -s.n_customers)

    def _transfer_view(self, since: float | None = None) -> nx.DiGraph:
        return nx.subgraph_view(self.g, filter_node=lambda n: self.g.nodes[n].get("kind") == "account",
                                filter_edge=lambda u, v: self._transfer_ok(u, v, since))

    def transaction_path(self, source_account: str, target_account: str, max_hops: int = 6) -> PathResult:
        s, t = node_id("account", source_account), node_id("account", target_account)
        if s == t or s not in self.g or t not in self.g:
            return PathResult(found=False)
        view = self._transfer_view()
        try:
            path = nx.shortest_path(view, s, t)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return PathResult(found=False)
        if len(path) - 1 > max_hops:
            return PathResult(found=False)
        edges = [self._edge(u, v) for u, v in zip(path[:-1], path[1:])]
        return PathResult(found=True, nodes=path, edges=edges, hops=len(path) - 1)

    def connected_accounts(self, account_id: str, max_hops: int = 2, limit: int = 100) -> list[ConnectedAccount]:
        start = node_id("account", account_id)
        if start not in self.g:
            return []
        out: dict[str, ConnectedAccount] = {}

        def owner(a: str) -> str | None:
            for p in self.g.predecessors(a):
                if self.g.nodes[p].get("kind") == "customer":
                    return split_node_id(p)[1]
            return None

        own = owner(start)
        # same owner
        if own:
            for a in self.g.successors(node_id("customer", own)):
                if self.g.nodes[a].get("kind") == "account" and a != start:
                    out[a] = ConnectedAccount(account_id=split_node_id(a)[1], owner_customer_id=own, hops=0,
                                              via="same_owner")
            # shared devices
            for sd in self.shared_devices(own):
                for c in sd.customers:
                    if c == own:
                        continue
                    for a in self.g.successors(node_id("customer", c)):
                        if self.g.nodes[a].get("kind") == "account" and a not in out:
                            out[a] = ConnectedAccount(account_id=split_node_id(a)[1], owner_customer_id=c, hops=1,
                                                      via=f"shared_device:{sd.device_id}")
        view = self._transfer_view().to_undirected(as_view=True)
        lengths = nx.single_source_shortest_path_length(view, start, cutoff=max_hops)
        for a, h in sorted(lengths.items(), key=lambda kv: kv[1]):
            if a == start or a in out:
                continue
            out[a] = ConnectedAccount(account_id=split_node_id(a)[1], owner_customer_id=owner(a), hops=h,
                                      via="transfers")
            if len(out) >= limit:
                break
        return sorted(out.values(), key=lambda c: (c.hops, c.account_id))[:limit]

    def suspicious_cluster(self, customer_id: str, max_hops: int = 2) -> Cluster:
        center = node_id("customer", customer_id)
        if center not in self.g:
            return Cluster(center=center, members=[], customers=[], accounts=[], devices=[], density=0.0,
                           flagged_customers=[], central_entities=[], shared_devices=[])
        allowed = {"customer", "account", "device", "phone", "email", "address"}
        seen = {center: 0}
        q = deque([center])
        while q and len(seen) < 400:
            n = q.popleft()
            if seen[n] >= max_hops * 2:  # customer->account->account->customer = 3 edges per "hop"
                continue
            if n != center and self.g.degree(n) > HUB_DEGREE_LIMIT:
                continue
            for m in list(self.g.successors(n)) + list(self.g.predecessors(n)):
                if m not in seen and self.g.nodes[m].get("kind") in allowed:
                    seen[m] = seen[n] + 1
                    q.append(m)
        sub = self.g.subgraph(seen).to_undirected()
        customers = sorted(split_node_id(n)[1] for n in sub if self.g.nodes[n].get("kind") == "customer")
        accounts = sorted(split_node_id(n)[1] for n in sub if self.g.nodes[n].get("kind") == "account")
        devices = sorted(split_node_id(n)[1] for n in sub if self.g.nodes[n].get("kind") == "device")
        flagged = sorted(split_node_id(n)[1] for n in sub
                         if self.g.nodes[n].get("kind") == "customer" and self.g.nodes[n].get("open_alert"))
        if sub.number_of_nodes() <= 400 and sub.number_of_nodes() > 2:
            cent = nx.betweenness_centrality(sub, normalized=True)
        else:
            cent = nx.degree_centrality(sub) if sub.number_of_nodes() > 1 else {}
        top = sorted(cent.items(), key=lambda kv: -kv[1])[:5]
        central = [{"node": n, "kind": self.g.nodes[n].get("kind"), "centrality": round(v, 4)} for n, v in top]
        shared = self.shared_devices(customer_id)
        return Cluster(center=center, members=sorted(sub.nodes), customers=customers, accounts=accounts,
                       devices=devices, density=round(nx.density(sub), 4) if sub.number_of_nodes() > 1 else 0.0,
                       flagged_customers=flagged, central_entities=central, shared_devices=shared)

    def trace_funds(self, account_id: str, direction: str = "out", max_hops: int = 3,
                    since: datetime | None = None, limit: int = 50) -> list[FundFlow]:
        start = node_id("account", account_id)
        if start not in self.g:
            return []
        since_e = _epoch(since)
        flows: list[FundFlow] = []

        def nbrs(n: str):
            it = self.g.successors(n) if direction == "out" else self.g.predecessors(n)
            for m in it:
                u, v = (n, m) if direction == "out" else (m, n)
                if self._transfer_ok(u, v, since_e):
                    yield m, self.g.edges[u, v]

        stack: list[tuple[list[str], list[dict]]] = [([start], [])]
        while stack and len(flows) < limit * 4:
            path, eds = stack.pop()
            for m, d in nbrs(path[-1]):
                if m in path:
                    continue
                # temporal plausibility on aggregated edges: next hop must have activity
                # after the previous hop started
                if eds and (d.get("last_epoch") or 0) < (eds[-1].get("first_epoch") or 0):
                    continue
                np_, ne = path + [m], eds + [d]
                flows.append(FundFlow(
                    path=[split_node_id(x)[1] for x in np_], hops=len(ne),
                    total_usd_first_hop=float(ne[0].get("total_usd", 0)),
                    min_edge_usd=float(min(e.get("total_usd", 0) for e in ne)),
                    first_ts=ne[0].get("first_ts"), last_ts=ne[-1].get("last_ts"),
                    sample_txn_ids=[t for e in ne for t in (e.get("sample_txn_ids") or [])[:2]]))
                if len(ne) < max_hops:
                    stack.append((np_, ne))
        flows.sort(key=lambda f: (-f.hops, -f.min_edge_usd))
        return flows[:limit]

    def candidate_cycles(self, account_ids: list[str], max_len: int = 5,
                         since: datetime | None = None) -> list[list[str]]:
        since_e = _epoch(since)
        starts = [node_id("account", a) for a in account_ids if node_id("account", a) in self.g]
        found: set[tuple[str, ...]] = set()
        for s in starts:
            stack: list[list[str]] = [[s]]
            while stack:
                path = stack.pop()
                for m in self.g.successors(path[-1]):
                    if not self._transfer_ok(path[-1], m, since_e):
                        continue
                    if m == s and len(path) >= 2:
                        cyc = [split_node_id(x)[1] for x in path]
                        k = cyc.index(min(cyc))
                        found.add(tuple(cyc[k:] + cyc[:k]))
                    elif m not in path and len(path) < max_len:
                        stack.append(path + [m])
        return [list(c) for c in sorted(found)]

    def counterparties(self, account_ids: list[str], since: datetime | None = None) -> list[str]:
        since_e = _epoch(since)
        out: set[str] = set()
        for a in account_ids:
            n = node_id("account", a)
            if n not in self.g:
                continue
            for m in self.g.successors(n):
                if self._transfer_ok(n, m, since_e):
                    out.add(split_node_id(m)[1])
            for m in self.g.predecessors(n):
                if self.g.nodes[m].get("kind") == "account" and self._transfer_ok(m, n, since_e):
                    out.add(split_node_id(m)[1])
        return sorted(out - set(account_ids))

    def account_owner(self, account_id: str) -> str | None:
        n = node_id("account", account_id)
        if n not in self.g:
            return None
        for p in self.g.predecessors(n):
            if self.g.nodes[p].get("kind") == "customer":
                return split_node_id(p)[1]
        return None

    def customer_flags(self, customer_ids: list[str]) -> dict[str, bool]:
        return {c: bool(self.g.nodes.get(node_id("customer", c), {}).get("open_alert")) for c in customer_ids}

    # ------------------------------------------------------- investigation queries (v2)
    def _own_accounts(self, customer_id: str) -> list[str]:
        c = node_id("customer", customer_id)
        if c not in self.g:
            return []
        return sorted(split_node_id(a)[1] for a in self.g.successors(c) if self.g.nodes[a].get("kind") == "account")

    def customer_counterparties(self, customer_id: str, degree: int = 1, since: datetime | None = None,
                                limit: int = 100) -> list[dict[str, Any]]:
        """Accounts that exchanged transfers with the customer (degree 1) or with those accounts (degree 2).

        Only account-to-account transfers exist in the projection; external beneficiaries are not nodes.
        """
        since_e = _epoch(since)
        own = set(self._own_accounts(customer_id))
        if not own:
            return []
        found: dict[str, dict[str, Any]] = {}

        def add(acc: str, deg: int, direction: str, edge: dict[str, Any], via: str | None) -> None:
            cur = found.get(acc)
            if cur is None:
                found[acc] = {"account_id": acc, "owner_customer_id": self.account_owner(acc), "degree": deg,
                              "direction": direction, "n_transactions": int(edge.get("n", 0)),
                              "total_usd": float(edge.get("total_usd", 0.0)), "via_account": via}
            elif cur["degree"] == deg:
                cur["n_transactions"] += int(edge.get("n", 0))
                cur["total_usd"] = round(cur["total_usd"] + float(edge.get("total_usd", 0.0)), 2)
                if cur["direction"] != direction:
                    cur["direction"] = "both"

        for a in own:
            n = node_id("account", a)
            for m in self.g.successors(n):
                if self._transfer_ok(n, m, since_e) and split_node_id(m)[1] not in own:
                    add(split_node_id(m)[1], 1, "out", self.g.edges[n, m], None)
            for m in self.g.predecessors(n):
                if (self.g.nodes[m].get("kind") == "account" and self._transfer_ok(m, n, since_e)
                        and split_node_id(m)[1] not in own):
                    add(split_node_id(m)[1], 1, "in", self.g.edges[m, n], None)
        if degree >= 2:
            first = [k for k, v in found.items() if v["degree"] == 1]
            for a in first:
                if len(found) >= limit * 3:
                    break
                n = node_id("account", a)
                if self._is_hub(n):
                    continue
                for m in self.g.successors(n):
                    acc = split_node_id(m)[1]
                    if self._transfer_ok(n, m, since_e) and acc not in own and acc not in found:
                        add(acc, 2, "out", self.g.edges[n, m], a)
                for m in self.g.predecessors(n):
                    acc = split_node_id(m)[1]
                    if (self.g.nodes[m].get("kind") == "account" and self._transfer_ok(m, n, since_e)
                            and acc not in own and acc not in found):
                        add(acc, 2, "in", self.g.edges[m, n], a)
        out = sorted(found.values(), key=lambda r: (r["degree"], -r["total_usd"], r["account_id"]))
        return out[:limit]

    def shared_beneficiaries(self, customer_id: str, since: datetime | None = None, min_other: int = 1,
                             limit: int = 50) -> list[dict[str, Any]]:
        """Beneficiary accounts the customer pays that are also paid by other customers."""
        since_e = _epoch(since)
        own = set(self._own_accounts(customer_id))
        out: list[dict[str, Any]] = []
        for a in own:
            n = node_id("account", a)
            for ben in self.g.successors(n):
                if not self._transfer_ok(n, ben, since_e):
                    continue
                ben_id = split_node_id(ben)[1]
                if ben_id in own:
                    continue
                others: dict[str, dict[str, Any]] = {}
                for src in self.g.predecessors(ben):
                    src_id = split_node_id(src)[1]
                    if src == n or src_id in own or self.g.nodes[src].get("kind") != "account":
                        continue
                    if not self._transfer_ok(src, ben, since_e):
                        continue
                    owner = self.account_owner(src_id) or src_id
                    d = others.setdefault(owner, {"customer_id": owner, "accounts": [], "n_transactions": 0,
                                                  "total_usd": 0.0})
                    d["accounts"].append(src_id)
                    d["n_transactions"] += int(self.g.edges[src, ben].get("n", 0))
                    d["total_usd"] = round(d["total_usd"] + float(self.g.edges[src, ben].get("total_usd", 0.0)), 2)
                if len(others) >= min_other:
                    out.append({"beneficiary_account": ben_id, "beneficiary_owner": self.account_owner(ben_id),
                                "paid_from": a, "n_other_customers": len(others),
                                "customers": sorted(others.values(), key=lambda d: -d["total_usd"])[:20]})
        out.sort(key=lambda r: (-r["n_other_customers"], r["beneficiary_account"]))
        return out[:limit]

    def common_recipients(self, account_ids: list[str], min_sources: int = 2, since: datetime | None = None,
                          limit: int = 50) -> list[dict[str, Any]]:
        """Accounts that receive funds from at least `min_sources` of the given (for example flagged) accounts."""
        since_e = _epoch(since)
        src_set = {node_id("account", a) for a in account_ids if node_id("account", a) in self.g}
        counts: dict[str, dict[str, Any]] = {}
        for s in src_set:
            for m in self.g.successors(s):
                if m in src_set or not self._transfer_ok(s, m, since_e):
                    continue
                d = counts.setdefault(m, {"sources": [], "total_usd": 0.0, "n_transactions": 0})
                d["sources"].append(split_node_id(s)[1])
                d["total_usd"] = round(d["total_usd"] + float(self.g.edges[s, m].get("total_usd", 0.0)), 2)
                d["n_transactions"] += int(self.g.edges[s, m].get("n", 0))
        rows = [{"account_id": split_node_id(m)[1], "owner_customer_id": self.account_owner(split_node_id(m)[1]),
                 "sources": sorted(d["sources"]), "n_sources": len(d["sources"]), "total_usd": d["total_usd"],
                 "n_transactions": d["n_transactions"]}
                for m, d in counts.items() if len(d["sources"]) >= min_sources]
        rows.sort(key=lambda r: (-r["n_sources"], -r["total_usd"], r["account_id"]))
        return rows[:limit]

    # ------------------------------------------------------- money-mule investigation queries (v3)
    def fan_patterns(self, since: datetime | None = None, min_degree: int = 5, limit: int = 50) -> list[dict[str, Any]]:
        """Accounts with many distinct transfer senders (fan-in) or many distinct receivers (fan-out)."""
        since_e = _epoch(since)
        rows: list[dict[str, Any]] = []
        for n, kind in self.g.nodes(data="kind"):
            if kind != "account":
                continue
            senders = [m for m in self.g.predecessors(n)
                       if self.g.nodes[m].get("kind") == "account" and self._transfer_ok(m, n, since_e)]
            receivers = [m for m in self.g.successors(n)
                         if self.g.nodes[m].get("kind") == "account" and self._transfer_ok(n, m, since_e)]
            if len(senders) < min_degree and len(receivers) < min_degree:
                continue
            acc = split_node_id(n)[1]
            owner = self.account_owner(acc)
            in_usd = sum(float(self.g.edges[m, n].get("total_usd", 0.0)) for m in senders)
            out_usd = sum(float(self.g.edges[n, m].get("total_usd", 0.0)) for m in receivers)
            pattern = ("fan_in+fan_out" if len(senders) >= min_degree and len(receivers) >= min_degree
                       else "fan_in" if len(senders) >= min_degree else "fan_out")
            rows.append({"account_id": acc, "owner_customer_id": owner, "pattern": pattern,
                         "distinct_senders": len(senders), "distinct_receivers": len(receivers),
                         "in_usd": round(in_usd, 2), "out_usd": round(out_usd, 2),
                         "flagged": bool(owner and self.customer_flags([owner]).get(owner))})
        rows.sort(key=lambda r: (-(r["distinct_senders"] + r["distinct_receivers"]), r["account_id"]))
        return rows[:limit]

    def flow_subgraph(self, customer_id: str, depth: int = 2, since: datetime | None = None, max_nodes: int = 60,
                      max_edges: int = 250) -> dict[str, Any]:
        """Account-to-account transfer neighbourhood of a customer: nodes with their depth and role, edges with
        amount, count and first/last time. Hubs are not expanded. Bounded; `truncated` says when a bound was hit."""
        since_e = _epoch(since)
        own = self._own_accounts(customer_id)
        if not own:
            return {"nodes": [], "edges": [], "truncated": False}
        depth_of: dict[str, int] = {node_id("account", a): 0 for a in own}
        role: dict[str, set[str]] = {n: {"subject"} for n in depth_of}
        edges: dict[tuple[str, str], dict[str, Any]] = {}
        frontier, truncated = list(depth_of), False
        for d in range(1, depth + 1):
            nxt: list[str] = []
            for n in frontier:
                if d > 1 and self._is_hub(n):
                    continue
                for direction in ("out", "in"):
                    it = self.g.successors(n) if direction == "out" else self.g.predecessors(n)
                    for m in it:
                        u, v = (n, m) if direction == "out" else (m, n)
                        if self.g.nodes[m].get("kind") != "account" or not self._transfer_ok(u, v, since_e):
                            continue
                        if m not in depth_of:
                            if len(depth_of) >= max_nodes:
                                truncated = True
                                continue
                            depth_of[m] = d
                            role[m] = {"downstream" if direction == "out" else "upstream"}
                            nxt.append(m)
                        elif depth_of[m] == d:
                            role[m].add("downstream" if direction == "out" else "upstream")
                        if (u, v) not in edges:
                            if len(edges) >= max_edges:
                                truncated = True
                                continue
                            e = self.g.edges[u, v]
                            edges[(u, v)] = {"source": split_node_id(u)[1], "target": split_node_id(v)[1],
                                             "total_usd": float(e.get("total_usd", 0.0)), "n": int(e.get("n", 0)),
                                             "first_ts": e.get("first_ts"), "last_ts": e.get("last_ts"),
                                             "depth": d, "direction": direction,
                                             "sample_txn_ids": list(e.get("sample_txn_ids") or [])[:3]}
            frontier = nxt
        owners = {n: self.account_owner(split_node_id(n)[1]) for n in depth_of}
        flags = self.customer_flags(sorted({o for o in owners.values() if o}))
        nodes = [{"account_id": split_node_id(n)[1], "owner_customer_id": owners[n], "depth": depth_of[n],
                  "roles": sorted(role[n]), "flagged": bool(owners[n] and flags.get(owners[n] or "")),
                  "is_hub": self._is_hub(n),
                  "in_degree": sum(1 for m in self.g.predecessors(n) if self._transfer_ok(m, n, since_e)),
                  "out_degree": sum(1 for m in self.g.successors(n) if self._transfer_ok(n, m, since_e))}
                 for n in sorted(depth_of, key=lambda x: (depth_of[x], x))]
        return {"nodes": nodes, "edges": sorted(edges.values(), key=lambda e: (e["depth"], -e["total_usd"])),
                "truncated": truncated}
