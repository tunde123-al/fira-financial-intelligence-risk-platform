# Graph model

## Projection, not replication

PostgreSQL remains the system of record. The graph holds **relationships only**, aggregated per
pair. Copying 250k transaction rows as nodes would make traversals slower and duplicate data that
SQL already serves well.

```
(:Customer {id, segment, customer_type, risk_profile, country, open_alert})
   -[:OWNS]->            (:Account {id})
   -[:USES_DEVICE {n, first_ts, last_ts, first_epoch, last_epoch}]-> (:Device {id})
   -[:USES_IP {n}]->     (:IPAddress {id})
   -[:HAS_IDENTIFIER]->  (:Phone|:Email|:Address {id: hash prefix, masked})
(:Account)-[:TRANSFERRED_TO {n, total_usd, first_ts, last_ts, first_epoch, last_epoch, sample_txn_ids}]->(:Account)
(:Account)-[:PAID {n, total_usd}]->(:Merchant {id, name, category, risk_profile, country})
```

- Edges carry time bounds (`first_epoch` / `last_epoch`), so window-restricted questions ("cycles
  active since the window start") need no transaction nodes.
- `sample_txn_ids` links every transfer edge back to concrete transactions for citation.
- Investigation and Alert are represented through the `open_alert` flag and the relational tables.
  Linking them as nodes was not needed by any query; it is a straightforward extension.
- Identifier nodes carry only a hash prefix and a masked value.

Uniqueness constraints are created on `id` for every label. The loader
(`python -m app.graph.neo4j_backend load`) rebuilds the projection with batched `UNWIND … MERGE`.

## Questions answered

| Tool | Answer | Implementation |
|---|---|---|
| `get_related_entities` | bounded neighbourhood (depth ≤ 3, node limit) | BFS; merchants, IPs and nodes with degree > 60 are included but **not traversed** (hub suppression, otherwise every customer is two hops from every other through a supermarket) |
| `find_shared_device` | devices this customer shares and with whom | Device in-degree |
| `trace_transaction_path` | shortest directed transfer path | `shortestPath` / BFS on the account subgraph |
| `find_connected_accounts` | same-owner, shared-device and transfer-connected accounts (≤ k hops) | |
| `find_suspicious_cluster` | customers, accounts and devices around the subject; density; flagged members; **betweenness centrality** (top 5); shared devices | |
| `trace_funds` | multi-hop outflow (or inflow) paths with a temporal plausibility check (each hop active after the previous began) | |
| cycles (used by CIRCULAR_FLOW) | simple cycles of length 2–5 through the subject's accounts, restricted to recent edges | DFS / `(a)-[*2..5]->(a)`; then verified on transactions for time order and amount similarity |
| connected components / depth | via the cluster and neighbourhood tools | |

## Two backends, one answer

`NetworkXGraph` builds the projection in memory from `DataStore.graph_edges()`. `Neo4jGraph` runs
bounded Cypher to fetch the local neighbourhood and then **reuses the NetworkX algorithms** on that
subgraph, so both backends return identical results on identical data.
`tests/integration/test_services.py::Neo4jGraphTest` checks this parity (node counts, shared
devices, cycles) in CI.

Neo4j is used for scale and for interactive exploration (the Neo4j Browser on :7474). Bounded
fetches keep each query's cost proportional to the neighbourhood size, not the graph size.
