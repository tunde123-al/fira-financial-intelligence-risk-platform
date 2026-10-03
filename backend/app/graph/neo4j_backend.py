"""Neo4j graph backend and projection loader.

Design choice: Cypher is used for what Neo4j is good at — index lookups and
bounded traversals over large graphs. Each query fetches a *bounded* local
neighbourhood; analytics that need a whole subgraph (centrality, density) are
then computed with the same NetworkX code the in-process backend uses, so both
backends give identical answers on identical data.

Load / rebuild the projection:
    python -m app.graph.neo4j_backend load
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from typing import Any

import networkx as nx
import pandas as pd

from app.graph.base import (
    Cluster,
    ConnectedAccount,
    FundFlow,
    GraphEdge,
    PathResult,
    SharedDevice,
    Subgraph,
    node_id,
)
from app.graph.networkx_backend import NetworkXGraph, _epoch, _iso

LABELS = {"customer": "Customer", "account": "Account", "merchant": "Merchant", "device": "Device",
          "ip": "IPAddress", "phone": "Phone", "email": "Email", "address": "Address"}
KIND_OF_LABEL = {v: k for k, v in LABELS.items()}
MAX_DEPTH = 4

CONSTRAINTS = [f"CREATE CONSTRAINT {k}_id IF NOT EXISTS FOR (n:{v}) REQUIRE n.id IS UNIQUE" for k, v in LABELS.items()]

LOAD_QUERIES = {
    "customers": "UNWIND $rows AS r MERGE (c:Customer {id: r.customer_id}) SET c.segment = r.segment, "
                 "c.customer_type = r.customer_type, c.risk_profile = r.risk_profile, c.country = r.country, "
                 "c.open_alert = r.open_alert",
    "merchants": "UNWIND $rows AS r MERGE (m:Merchant {id: r.merchant_id}) SET m.name = r.name, m.category = r.category, "
                 "m.risk_profile = r.risk_profile, m.country = r.country",
    "ownership": "UNWIND $rows AS r MERGE (c:Customer {id: r.customer_id}) MERGE (a:Account {id: r.account_id}) "
                 "MERGE (c)-[:OWNS]->(a)",
    "transfers": "UNWIND $rows AS r MATCH (a:Account {id: r.sender_account_id}) MATCH (b:Account {id: r.receiver_account_id}) "
                 "MERGE (a)-[t:TRANSFERRED_TO]->(b) SET t.n = r.n, t.total_usd = r.total_usd, t.first_ts = r.first_ts, "
                 "t.last_ts = r.last_ts, t.first_epoch = r.first_epoch, t.last_epoch = r.last_epoch, "
                 "t.sample_txn_ids = r.sample_txn_ids",
    "device_usage": "UNWIND $rows AS r MATCH (c:Customer {id: r.customer_id}) MERGE (d:Device {id: r.device_id}) "
                    "MERGE (c)-[u:USES_DEVICE]->(d) SET u.n = r.n, u.first_ts = r.first_ts, u.last_ts = r.last_ts, "
                    "u.first_epoch = r.first_epoch, u.last_epoch = r.last_epoch",
    "ip_usage": "UNWIND $rows AS r MATCH (c:Customer {id: r.customer_id}) MERGE (i:IPAddress {id: r.ip_address}) "
                "MERGE (c)-[u:USES_IP]->(i) SET u.n = r.n",
    "merchant_payments": "UNWIND $rows AS r MATCH (a:Account {id: r.account_id}) MATCH (m:Merchant {id: r.merchant_id}) "
                         "MERGE (a)-[p:PAID]->(m) SET p.n = r.n, p.total_usd = r.total_usd",
}
IDENTIFIER_QUERY = ("UNWIND $rows AS r MATCH (c:Customer {{id: r.customer_id}}) MERGE (x:{label} {{id: r.hid}}) "
                    "SET x.masked = r.masked_value MERGE (c)-[:HAS_IDENTIFIER]->(x)")


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    out = []
    for r in df.to_dict("records"):
        clean = {}
        for k, v in r.items():
            if isinstance(v, float) and pd.isna(v):
                v = None
            elif isinstance(v, pd.Timestamp):
                v = v.isoformat()
            elif hasattr(v, "item"):
                v = v.item()
            clean[k] = v
        out.append(clean)
    return out


class Neo4jGraph:
    name = "neo4j"

    def __init__(self, uri: str, user: str, password: str, database: str | None = None):
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.database = database

    def close(self) -> None:
        self.driver.close()

    def _q(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        with self.driver.session(database=self.database) as s:
            return s.run(cypher, **params).data()

    # ---------------------------------------------------------------- loading
    def load_projection(self, edges: dict[str, pd.DataFrame], batch: int = 5000, rebuild: bool = True) -> dict[str, int]:
        if rebuild:
            while self._q("MATCH (n) WITH n LIMIT 20000 DETACH DELETE n RETURN count(*) AS c")[0]["c"]:
                pass
        for c in CONSTRAINTS:
            self._q(c)
        flagged = set()
        al = edges.get("alerts")
        if al is not None and not al.empty:
            flagged = set(al[(al.status == "open") & (al.entity_type == "customer")].entity_id)
        cust = edges["customers"].assign(open_alert=lambda d: d.customer_id.isin(flagged))
        tr = edges["transfers"].copy()
        tr["first_epoch"] = tr.first_ts.map(_epoch)
        tr["last_epoch"] = tr.last_ts.map(_epoch)
        tr["first_ts"] = tr.first_ts.map(_iso)
        tr["last_ts"] = tr.last_ts.map(_iso)
        du = edges["device_usage"].copy()
        du["first_epoch"] = du.first_ts.map(_epoch)
        du["last_epoch"] = du.last_ts.map(_epoch)
        du["first_ts"] = du.first_ts.map(_iso)
        du["last_ts"] = du.last_ts.map(_iso)
        frames = {"customers": cust, "merchants": edges["merchants"], "ownership": edges["ownership"],
                  "transfers": tr, "device_usage": du, "ip_usage": edges["ip_usage"],
                  "merchant_payments": edges["merchant_payments"]}
        counts = {}
        for key, df in frames.items():
            rows = _records(df)
            for i in range(0, len(rows), batch):
                self._q(LOAD_QUERIES[key], rows=rows[i:i + batch])
            counts[key] = len(rows)
        ident = edges["identifiers"].assign(hid=lambda d: d.value_hash.str[:16])
        for t, df in ident.groupby("identifier_type"):
            rows = _records(df)
            for i in range(0, len(rows), batch):
                self._q(IDENTIFIER_QUERY.format(label=LABELS[t]), rows=rows[i:i + batch])
            counts[f"identifiers_{t}"] = len(rows)
        return counts

    # ------------------------------------------------------- local subgraphs
    def _local_graph(self, cypher: str, **params: Any) -> nx.DiGraph:
        """Run a query returning `nodes` and `rels` lists and build a DiGraph."""
        g = nx.DiGraph()
        for row in self._q(cypher, **params):
            for n in row.get("nodes") or []:
                kind = KIND_OF_LABEL.get(n["label"], n["label"].lower())
                props = dict(n.get("props") or {})
                nid = node_id(kind, props.pop("id"))
                label = props.pop("name", None) or props.pop("masked", None) or nid.split(":", 1)[1]
                g.add_node(nid, kind=kind, label=label, **props)
            for r in row.get("rels") or []:
                s = node_id(KIND_OF_LABEL[r["src_label"]], r["src"])
                t = node_id(KIND_OF_LABEL[r["dst_label"]], r["dst"])
                g.add_edge(s, t, rel=r["type"], **(r.get("props") or {}))
        return g

    _PATH_PROJECTION = (
        "WITH collect(p) AS paths "
        "UNWIND paths AS pp UNWIND nodes(pp) AS n WITH paths, collect(DISTINCT n) AS ns "
        "UNWIND paths AS pp UNWIND relationships(pp) AS r WITH ns, collect(DISTINCT r) AS rs "
        "RETURN [n IN ns | {label: labels(n)[0], props: properties(n)}] AS nodes, "
        "[r IN rs | {type: type(r), src: startNode(r).id, src_label: labels(startNode(r))[0], "
        "dst: endNode(r).id, dst_label: labels(endNode(r))[0], props: properties(r)}] AS rels")

    def _neighbourhood(self, kind: str, natural_id: str, depth: int, limit: int,
                       rels: str | None = None) -> NetworkXGraph:
        depth = max(1, min(int(depth), MAX_DEPTH))
        label = LABELS[kind]
        rel = f":{rels}" if rels else ""
        cypher = (f"MATCH (c:{label} {{id: $id}}) "
                  f"MATCH p = (c)-[{rel}*1..{depth}]-(n) "
                  "WHERE none(x IN nodes(p)[1..-1] WHERE x:Merchant OR x:IPAddress OR COUNT {{ (x)--() }} > 60) "
                  f"WITH p LIMIT $limit " + self._PATH_PROJECTION)
        return NetworkXGraph(self._local_graph(cypher, id=natural_id, limit=limit * 4))

    # ------------------------------------------------------------- interface
    def ping(self) -> bool:
        return self._q("RETURN 1 AS ok")[0]["ok"] == 1

    def stats(self) -> dict[str, Any]:
        nodes = self._q("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS c")
        rels = self._q("MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c")
        return {"backend": self.name, "nodes": sum(r["c"] for r in nodes), "edges": sum(r["c"] for r in rels),
                "node_kinds": {KIND_OF_LABEL.get(r["label"], r["label"]): r["c"] for r in nodes},
                "edge_types": {r["t"]: r["c"] for r in rels}}

    def related_entities(self, kind: str, natural_id: str, depth: int = 2, limit: int = 150) -> Subgraph:
        local = self._neighbourhood(kind, natural_id, depth, limit)
        return local.related_entities(kind, natural_id, depth, limit)

    def shared_devices(self, customer_id: str, min_customers: int = 2) -> list[SharedDevice]:
        rows = self._q("MATCH (c:Customer {id: $id})-[:USES_DEVICE]->(d:Device)<-[:USES_DEVICE]-(o:Customer) "
                       "WITH d, collect(DISTINCT o.id) + [$id] AS users WHERE size(users) >= $min "
                       "RETURN d.id AS device_id, users ORDER BY size(users) DESC", id=customer_id, min=min_customers)
        return [SharedDevice(device_id=r["device_id"], customers=sorted(set(r["users"])),
                             n_customers=len(set(r["users"]))) for r in rows]

    def transaction_path(self, source_account: str, target_account: str, max_hops: int = 6) -> PathResult:
        hops = max(1, min(int(max_hops), 8))
        rows = self._q(f"MATCH (a:Account {{id: $s}}), (b:Account {{id: $t}}) "
                       f"MATCH p = shortestPath((a)-[:TRANSFERRED_TO*..{hops}]->(b)) "
                       "RETURN [n IN nodes(p) | n.id] AS ids, [r IN relationships(p) | properties(r)] AS props",
                       s=source_account, t=target_account)
        if not rows:
            return PathResult(found=False)
        ids = [node_id("account", i) for i in rows[0]["ids"]]
        edges = []
        for (u, v), pr in zip(zip(ids[:-1], ids[1:]), rows[0]["props"]):
            pr = {k: v2 for k, v2 in pr.items() if k not in ("first_epoch", "last_epoch")}
            edges.append(GraphEdge(source=u, target=v, rel="TRANSFERRED_TO", props=pr))
        return PathResult(found=True, nodes=ids, edges=edges, hops=len(edges))

    def connected_accounts(self, account_id: str, max_hops: int = 2, limit: int = 100) -> list[ConnectedAccount]:
        local = self._neighbourhood("account", account_id, max(2, max_hops * 2), 400,
                                    rels="TRANSFERRED_TO|OWNS|USES_DEVICE")
        return local.connected_accounts(account_id, max_hops, limit)

    def suspicious_cluster(self, customer_id: str, max_hops: int = 2) -> Cluster:
        local = self._neighbourhood("customer", customer_id, min(max_hops * 2, MAX_DEPTH), 400,
                                    rels="OWNS|TRANSFERRED_TO|USES_DEVICE|HAS_IDENTIFIER")
        return local.suspicious_cluster(customer_id, max_hops)

    def trace_funds(self, account_id: str, direction: str = "out", max_hops: int = 3,
                    since: datetime | None = None, limit: int = 50) -> list[FundFlow]:
        hops = max(1, min(int(max_hops), 5))
        arrow = f"-[rs:TRANSFERRED_TO*1..{hops}]->" if direction == "out" else f"<-[rs:TRANSFERRED_TO*1..{hops}]-"
        cypher = (f"MATCH p = (a:Account {{id: $id}}){arrow}(b:Account) "
                  "WHERE all(r IN rs WHERE r.last_epoch >= $since) WITH p LIMIT 2000 " + self._PATH_PROJECTION)
        local = NetworkXGraph(self._local_graph(cypher, id=account_id, since=_epoch(since) or 0.0))
        return local.trace_funds(account_id, direction, max_hops, since, limit)

    def candidate_cycles(self, account_ids: list[str], max_len: int = 5,
                         since: datetime | None = None) -> list[list[str]]:
        n = max(2, min(int(max_len), 6))
        rows = self._q(f"MATCH p = (a:Account)-[rs:TRANSFERRED_TO*2..{n}]->(a) WHERE a.id IN $ids "
                       "AND all(r IN rs WHERE r.last_epoch >= $since) "
                       "RETURN [x IN nodes(p) | x.id] AS ids LIMIT 500", ids=list(account_ids),
                       since=_epoch(since) or 0.0)
        found: set[tuple[str, ...]] = set()
        for r in rows:
            cyc = r["ids"][:-1]
            if len(set(cyc)) != len(cyc):
                continue  # not a simple cycle
            k = cyc.index(min(cyc))
            found.add(tuple(cyc[k:] + cyc[:k]))
        return [list(c) for c in sorted(found)]

    def counterparties(self, account_ids: list[str], since: datetime | None = None) -> list[str]:
        rows = self._q("MATCH (a:Account)-[r:TRANSFERRED_TO]-(o:Account) WHERE a.id IN $ids AND r.last_epoch >= $since "
                       "AND NOT o.id IN $ids RETURN DISTINCT o.id AS id", ids=list(account_ids),
                       since=_epoch(since) or 0.0)
        return sorted(r["id"] for r in rows)

    def account_owner(self, account_id: str) -> str | None:
        rows = self._q("MATCH (c:Customer)-[:OWNS]->(:Account {id: $id}) RETURN c.id AS id LIMIT 1", id=account_id)
        return rows[0]["id"] if rows else None

    def customer_flags(self, customer_ids: list[str]) -> dict[str, bool]:
        rows = self._q("MATCH (c:Customer) WHERE c.id IN $ids RETURN c.id AS id, coalesce(c.open_alert, false) AS f",
                       ids=list(customer_ids))
        out = {c: False for c in customer_ids}
        out.update({r["id"]: bool(r["f"]) for r in rows})
        return out


def main() -> None:
    from app.config import get_settings
    from app.services.container import build_store

    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["load", "stats"])
    a = p.parse_args()
    s = get_settings()
    if not (s.neo4j_uri and s.neo4j_user and s.neo4j_password):
        raise SystemExit("NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD must be set")
    g = Neo4jGraph(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    if a.cmd == "load":
        store = build_store(s)
        print(json.dumps(g.load_projection(store.graph_edges(None, None)), indent=2))
    print(json.dumps(g.stats(), indent=2))
    g.close()


if __name__ == "__main__":
    main()
