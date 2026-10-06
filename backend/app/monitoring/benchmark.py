"""Reproducible performance benchmark for the monitoring pipeline.

For each requested size it generates a synthetic bank with roughly that many transactions and measures, on the
machine it runs on: dataset load/ingestion, graph build, a daily-batch monitoring run, per-customer risk
assessment, alert generation, and the latency of the queries an investigator triggers.

Backends: the in-memory store always; PostgreSQL when --database-url points at a server (a throwaway database
is created and dropped). Only measured values are reported. Nothing is extrapolated.

    python -m app.monitoring.benchmark --sizes 10000 100000 --out ../docs/benchmarks/perf.json
    python -m app.monitoring.benchmark --sizes 10000 --database-url postgresql+psycopg://u:p@localhost:5432/postgres
"""
from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import tempfile
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any


def _rss_mb() -> dict[str, float | None]:
    """Resident memory of this process (psutil); peak working set is available on Windows only."""
    try:
        import psutil

        mi = psutil.Process().memory_info()
        peak = getattr(mi, "peak_wset", None)
        return {"rss_mb": round(mi.rss / 2**20, 1), "peak_mb": round(peak / 2**20, 1) if peak else None}
    except Exception:  # psutil is optional (dev dependency)
        return {"rss_mb": None, "peak_mb": None}


TX_PER_CUSTOMER = 25.4  # measured on the default generator: 254,197 transactions / 10,000 customers
DB_DIR = Path(__file__).resolve().parents[1] / "db"


def _ms(fn: Callable[[], Any], repeat: int = 1) -> tuple[Any, float]:
    t = time.perf_counter()
    out = None
    for _ in range(repeat):
        out = fn()
    return out, (time.perf_counter() - t) * 1000 / repeat


def _dist(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {}
    s = sorted(samples)
    return {"n": len(s), "mean_ms": round(statistics.mean(s), 2), "p50_ms": round(s[len(s) // 2], 2),
            "p95_ms": round(s[min(len(s) - 1, int(len(s) * 0.95))], 2), "max_ms": round(s[-1], 2)}


def _settings(dataset: Path, model_dir: Path, **over: Any) -> Any:
    from app.config import Settings

    base: dict[str, Any] = dict(environment="test", data_backend="frames", dataset_dir=dataset, model_dir=model_dir,
                                graph_backend="networkx", vector_backend="memory", llm_provider="none",
                                tool_timeout_s=120.0)
    base.update(over)
    return Settings(**base)


def _query_latencies(c: Any, customer: str, repeat: int = 20) -> dict[str, Any]:
    """Latency of the queries an investigator triggers (alert queue, detail, KPIs, evidence, graph)."""
    from app.monitoring.repository import AlertFilter
    from app.security.principal import Principal

    admin = Principal("U-bench", "admin")
    out: dict[str, Any] = {}
    items, total = c.monitoring_repo.list_alerts(AlertFilter(limit=50))
    out["alerts_in_store"] = total
    if items:
        a = items[0]
        out["alert_queue_page_50"] = _dist([_ms(lambda: c.monitoring_repo.list_alerts(AlertFilter(limit=50)))[1] for _ in range(repeat)])
        out["alert_queue_filtered"] = _dist([_ms(lambda: c.monitoring_repo.list_alerts(AlertFilter(
            statuses=["NEW"], severities=["high", "critical"], min_risk=30, sort="risk_score", limit=50)))[1] for _ in range(repeat)])
        out["alert_detail"] = _dist([_ms(lambda: c.monitoring.alert_detail(a.alert_id))[1] for _ in range(repeat)])
        cust = a.customer_id
    else:
        cust = customer
    out["kpis"] = _dist([_ms(c.monitoring_repo.kpis)[1] for _ in range(max(3, repeat // 4))])
    accounts = [x.account_id for x in c.store.accounts_for_customer(cust)]
    end = c.store.as_of()
    out["customer_transactions_90d"] = _dist([_ms(lambda: c.store.transactions_for_accounts(
        accounts, end - timedelta(days=90), end))[1] for _ in range(repeat)])
    if c.graph is not None and hasattr(c.graph, "customer_counterparties"):
        since = end - timedelta(days=90)
        out["graph_counterparties_degree2"] = _dist([_ms(lambda: c.graph.customer_counterparties(cust, 2, since, 60))[1] for _ in range(repeat)])
        out["graph_neighbourhood_depth2"] = _dist([_ms(lambda: c.graph.related_entities("customer", cust, 2, 120))[1] for _ in range(repeat)])
    # workbench assembly (customer profile, risk, rules, transactions, activity, graph, documents, timeline)
    if items:
        from app.api.workbench import build_workbench

        case, _ = c.monitoring.create_case(a.customer_id, [a.alert_id], admin) if not a.case_id else (c.monitoring_repo.get_case(a.case_id), False)
        out["case_workbench_assembly"] = _dist([_ms(lambda: build_workbench(c, admin, case.case_id))[1] for _ in range(5)])
    return out


def _measure_backend(label: str, c: Any, sample: list[str], batch: list[dict[str, Any]]) -> dict[str, Any]:
    """Detection, scoring, alert generation and query latency on one wired container."""
    from app.security.principal import Principal

    res: dict[str, Any] = {"backend": label}
    end = c.store.as_of()
    lookback, baseline = c.monitoring_config.lookback_days, c.monitoring_config.baseline_days
    # risk scoring alone, per customer
    scoring = []
    for cid in sample:
        t = time.perf_counter()
        c.risk_engine.assess_customer(cid, end, lookback, baseline)
        scoring.append((time.perf_counter() - t) * 1000)
    res["risk_scoring_per_customer"] = _dist(scoring)
    # a daily monitoring batch: customers with a transaction in the last day
    run = c.monitoring.run(window_end=end, active_days=1, mode="benchmark", max_customers=100_000)
    res["daily_batch"] = {
        "customers_screened": run.customers_evaluated, "transactions_in_scope": run.transactions_in_scope,
        "alerts_created": run.alerts_created, "alerts_merged": run.alerts_updated, "errors": run.errors,
        "duration_s": round(run.duration_ms / 1000, 2),
        "customers_per_second": round(run.customers_evaluated / max(run.duration_ms / 1000, 1e-6), 2),
        "timing_ms": run.details.get("timing_ms"),
        "ms_per_customer": round(run.duration_ms / max(run.customers_evaluated, 1), 2)}
    # idempotent re-run (everything already alerted: measures the merge path)
    rerun = c.monitoring.run(window_end=end, active_days=1, mode="benchmark", max_customers=100_000)
    res["daily_batch_rerun"] = {"duration_s": round(rerun.duration_ms / 1000, 2), "alerts_created": rerun.alerts_created,
                                "alerts_merged": rerun.alerts_updated, "timing_ms": rerun.details.get("timing_ms")}
    # API-style batch ingestion
    if batch:
        t = time.perf_counter()
        out = c.monitoring.ingest(batch, Principal("U-bench", "admin"), run_monitoring=False)
        dt = time.perf_counter() - t
        res["api_batch_ingestion"] = {"rows": len(batch), "accepted": out["accepted"], "rejected": out["rejected_count"],
                                      "seconds": round(dt, 3), "rows_per_second": round(out["accepted"] / max(dt, 1e-6), 1)}
    res["queries"] = _query_latencies(c, sample[0])
    res["v3"] = _measure_v3(c, sample)
    res["memory"] = {"after_run": _rss_mb()}
    return res


def _dirty_batch(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A batch with ~10% rows the gate must reject: bad amounts, unknown accounts, repeated ids."""
    out = [dict(r) for r in rows]
    for i in range(0, len(out), 10):
        kind = (i // 10) % 3
        if kind == 0:
            out[i]["amount"] = -1
        elif kind == 1:
            out[i]["sender_account_id"] = "ACC-99999999"
        else:
            out[i]["transaction_id"] = "TXN-777000001"
    return out


def _measure_v3(c: Any, sample: list[str]) -> dict[str, Any]:
    """Triage, money-mule, data-quality and operator-view timings."""
    from app.security.principal import Principal

    admin = Principal("U-bench", "admin")
    out: dict[str, Any] = {}
    res_t, ms_t = _ms(lambda: c.monitoring.recompute_triage(admin))
    out["triage_recompute_all_open"] = {**res_t, "ms": round(ms_t, 1)}
    mule = [_ms(partial(c.monitoring.mule_assessment, cid))[1] for cid in sample[:30]]
    out["mule_assessment_per_customer"] = _dist(mule)
    if c.graph is not None and hasattr(c.graph, "flow_subgraph"):
        out["mule_flow_depth2"] = _dist([_ms(partial(c.monitoring.mule_flow, cid, 2, 30))[1] for cid in sample[:30]])
        out["mule_fan_patterns"] = _dist([_ms(lambda: c.monitoring.mule_patterns(30, 5, 50))[1] for _ in range(5)])
    out["data_quality_summary"] = _dist([_ms(c.monitoring.data_quality_summary)[1] for _ in range(10)])
    out["alert_quality"] = _dist([_ms(c.monitoring.alert_quality)[1] for _ in range(10)])
    out["my_work"] = _dist([_ms(lambda: c.monitoring.my_work(admin))[1] for _ in range(10)])
    ids = c.store.list_customer_ids()
    rng = random.Random(7)
    batch = _dirty_batch(_synthetic_batch(c, 2000, rng))
    t = time.perf_counter()
    r = c.monitoring.ingest(batch, admin, run_monitoring=False, source="benchmark-dirty")
    dt = time.perf_counter() - t
    b = r["batch"]
    out["gate_with_rejections"] = {"rows": b["received"], "processed": b["processed"], "rejected": b["rejected"],
                                   "seconds": round(dt, 3), "rows_per_second": round(b["received"] / max(dt, 1e-6), 1),
                                   "accounting_balanced": b["received"] == b["processed"] + b["rejected"] + b["failed"]}
    out["customers_in_store"] = len(ids)
    return out


def _synthetic_batch(c: Any, n: int, rng: random.Random) -> list[dict[str, Any]]:
    from app.synthetic.reference import FX_PER_USD

    ids = c.store.list_customer_ids()
    rows = []
    end = c.store.as_of()
    for i in range(n):
        acc = c.store.accounts_for_customer(rng.choice(ids))[0]
        usd = rng.uniform(5, 400)
        rows.append({"timestamp": (end - timedelta(seconds=rng.randint(0, 3600))).isoformat(),
                     "sender_account_id": acc.account_id, "amount": round(usd * FX_PER_USD[acc.currency], 2),
                     "currency": acc.currency, "amount_usd": round(usd, 2), "transaction_type": "transfer",
                     "channel": "web", "status": "completed", "external_counterparty": f"EXT-BENCH-{i % 50}"})
    return rows


def run_size(n_tx: int, database_url: str | None, batch_rows: int, sample_n: int, workdir: Path) -> dict[str, Any]:
    from app.services.container import build_container
    from app.synthetic.generator import generate_dataset

    customers = max(300, round(n_tx / TX_PER_CUSTOMER))
    ds = workdir / f"bank_{n_tx}"
    t = time.perf_counter()
    manifest = generate_dataset(ds, n_customers=customers, seed=42)
    gen_s = time.perf_counter() - t
    actual = int(manifest["counts"]["transactions"])
    out: dict[str, Any] = {"target_transactions": n_tx, "customers": customers, "transactions": actual,
                           "dataset_generation_s": round(gen_s, 1), "backends": []}
    rng = random.Random(1)

    # ---------------- in-memory store
    try:
        mem_models = Path(tempfile.mkdtemp(prefix="fira-bench-models-"))
        t = time.perf_counter()
        c = build_container(_settings(ds, mem_models), load_ml=False)
        build_s = time.perf_counter() - t
        sample = rng.sample(c.store.list_customer_ids(), min(sample_n, customers))
        r = _measure_backend("frames (in-memory pandas)", c, sample, _synthetic_batch(c, batch_rows, rng))
        r["load_and_graph_build_s"] = round(build_s, 2)
        r["load_rate_transactions_per_s"] = round(actual / build_s, 0)
        out["backends"].append(r)
        del c
    except Exception as e:  # keep partial results
        out["backends"].append({"backend": "frames", "error": f"{type(e).__name__}: {e}"[:300]})

    # ---------------- PostgreSQL
    if database_url:
        try:
            out["backends"].append(_postgres(ds, database_url, actual, sample_n, batch_rows, rng))
        except Exception as e:
            out["backends"].append({"backend": "postgresql", "error": f"{type(e).__name__}: {e}"[:300]})
    return out


def _postgres(ds: Path, url: str, actual: int, sample_n: int, batch_rows: int, rng: random.Random) -> dict[str, Any]:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from app.db.loader import load
    from app.services.container import build_container

    name = f"fira_bench_{uuid.uuid4().hex[:8]}"
    admin = create_engine(make_url(url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))  # noqa: S608
    dburl = make_url(url).set(database=name).render_as_string(hide_password=False)
    try:
        eng = create_engine(dburl)
        with eng.begin() as conn:
            for f in ("schema.sql", "schema_audit_guard.sql", "schema_monitoring.sql",
                      "schema_production.sql"):
                conn.exec_driver_sql((DB_DIR / f).read_text(encoding="utf-8"))
        eng.dispose()
        t = time.perf_counter()
        counts = load(dburl, ds, truncate=False)
        load_s = time.perf_counter() - t
        t = time.perf_counter()
        c = build_container(_settings(ds, Path(tempfile.mkdtemp(prefix="fira-bench-models-")), data_backend="postgres",
                                      database_url=dburl), load_ml=False)
        build_s = time.perf_counter() - t
        sample = rng.sample(c.store.list_customer_ids(), min(sample_n, len(c.store.list_customer_ids())))
        r = _measure_backend("postgresql 15 (SqlStore)", c, sample, _synthetic_batch(c, batch_rows, rng))
        r["bulk_load_s"] = round(load_s, 2)
        r["bulk_load_rows"] = int(sum(counts.values()))
        r["bulk_load_transactions_per_s"] = round(actual / load_s, 0)
        r["container_and_graph_build_s"] = round(build_s, 2)
        c.store.engine.dispose()  # type: ignore[attr-defined]
        return r
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))  # noqa: S608


def to_markdown(res: dict[str, Any]) -> str:
    env = res["environment"]
    L = [f"# FIRA monitoring performance ({res['created_at'][:10]})", "",
         "Measured on one machine, single process, synthetic data. Numbers are for orientation, not a capacity guarantee. "
         "Nothing is extrapolated.", "",
         f"- machine: {env['platform']}, Python {env['python']}, {env['processor']}, {env.get('cpu_count')} logical CPUs",
         f"- sizes requested: {res['sizes']}; PostgreSQL: {res['database'] or 'not used'}", ""]
    for s in res["results"]:
        L += [f"## {s['transactions']:,} transactions ({s['customers']:,} customers; target {s['target_transactions']:,})", "",
              f"dataset generation (not a product metric): {s['dataset_generation_s']} s", ""]
        for b in s["backends"]:
            L += [f"### {b['backend']}", ""]
            if "error" in b:
                L += [f"FAILED: {b['error']}", ""]
                continue
            if "load_and_graph_build_s" in b:
                L.append(f"- load + graph build: {b['load_and_graph_build_s']} s ({b['load_rate_transactions_per_s']:,.0f} transactions/s)")
            if "bulk_load_s" in b:
                L.append(f"- bulk load (COPY): {b['bulk_load_s']} s ({b['bulk_load_transactions_per_s']:,.0f} transactions/s); "
                         f"container + graph build {b['container_and_graph_build_s']} s")
            if b.get("memory", {}).get("after_run", {}).get("rss_mb"):
                m = b["memory"]["after_run"]
                L.append(f"- memory of the benchmark process after the run: RSS {m['rss_mb']:.0f} MB"
                         + (f", peak working set {m['peak_mb']:.0f} MB" if m.get("peak_mb") else ""))
            sc = b["risk_scoring_per_customer"]
            L.append(f"- risk scoring per customer (n={sc['n']}): mean {sc['mean_ms']} ms, p50 {sc['p50_ms']} ms, p95 {sc['p95_ms']} ms")
            d = b["daily_batch"]
            L.append(f"- daily batch: {d['customers_screened']:,} customers with activity ({d['transactions_in_scope']:,} transactions) screened in "
                     f"{d['duration_s']} s = {d['customers_per_second']} customers/s ({d['ms_per_customer']} ms each); "
                     f"{d['alerts_created']} alerts created; stage time ms {d['timing_ms']}")
            r = b["daily_batch_rerun"]
            L.append(f"- same batch re-run (merge path, no new alerts expected): {r['duration_s']} s, created {r['alerts_created']}, merged {r['alerts_merged']}")
            if "api_batch_ingestion" in b:
                i = b["api_batch_ingestion"]
                L.append(f"- API-style batch ingestion: {i['accepted']:,} rows in {i['seconds']} s ({i['rows_per_second']:,.0f} rows/s), {i['rejected']} rejected")
            v3 = b.get("v3", {})
            if v3.get("gate_with_rejections"):
                g = v3["gate_with_rejections"]
                L.append(f"- ingestion gate with ~10% invalid rows: {g['rows']:,} rows in {g['seconds']} s ({g['rows_per_second']:,.0f} rows/s), "
                         f"{g['processed']:,} processed / {g['rejected']:,} rejected, accounting balanced: {g['accounting_balanced']}")
            if v3.get("triage_recompute_all_open"):
                t_ = v3["triage_recompute_all_open"]
                L.append(f"- triage re-score of all unresolved alerts: {t_['recomputed']:,} alerts in {t_['ms']} ms")
            L += ["", "| query | n | mean ms | p50 ms | p95 ms |", "|---|---|---|---|---|"]
            rows_ = {**b["queries"], **{k: v for k, v in v3.items() if isinstance(v, dict) and "p50_ms" in v}}
            for k, v in rows_.items():
                if isinstance(v, dict) and v:
                    L.append(f"| {k} | {v['n']} | {v['mean_ms']} | {v['p50_ms']} | {v['p95_ms']} |")
            L.append("")
    L += ["## Reproduce", "", "```bash", "cd backend",
          f"python -m app.monitoring.benchmark --sizes {' '.join(str(x) for x in res['sizes'])}"
          + (" --database-url postgresql+psycopg://USER:PASSWORD@HOST:5432/postgres" if res["database"] else ""), "```", ""]
    return "\n".join(L)


TRACKED = {  # metric path in a backend result -> (label, "lower"|"higher" is better)
    "risk_scoring_per_customer.p50_ms": ("risk scoring p50 (ms)", "lower"),
    "daily_batch.ms_per_customer": ("daily batch ms per customer", "lower"),
    "api_batch_ingestion.rows_per_second": ("batch ingestion rows/s", "higher"),
    "v3.gate_with_rejections.rows_per_second": ("ingestion gate rows/s with rejections", "higher"),
    "queries.alert_queue_page_50.p95_ms": ("alert queue page p95 (ms)", "lower"),
    "queries.case_workbench_assembly.p95_ms": ("case workbench p95 (ms)", "lower"),
    "v3.mule_assessment_per_customer.p95_ms": ("mule assessment p95 (ms)", "lower"),
}


def _dig(d: Any, path: str) -> float | None:
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return float(d) if isinstance(d, (int, float)) else None


def extract_metrics(res: dict[str, Any]) -> dict[str, float]:
    """Tracked metrics per (transactions, backend) from a benchmark result."""
    out: dict[str, float] = {}
    for size in res.get("results", []):
        for b in size.get("backends", []):
            if "error" in b:
                continue
            for path in TRACKED:
                v = _dig(b, path)
                if v is not None:
                    out[f"{size['transactions']}|{b['backend']}|{path}"] = v
    return out


def compare(baseline: dict[str, Any], current: dict[str, Any], tolerance: float = 0.5) -> list[dict[str, Any]]:
    """Regressions: a lower-is-better metric more than (1 + tolerance)x the baseline, or a higher-is-better one below
    baseline / (1 + tolerance). Tolerance is wide on purpose: timings on a shared machine vary run to run."""
    base, cur = extract_metrics(baseline), extract_metrics(current)
    out = []
    for key, b in base.items():
        if key not in cur or b <= 0:
            continue
        path = key.split("|")[2]
        better = TRACKED[path][1]
        ratio = cur[key] / b
        bad = ratio > 1 + tolerance if better == "lower" else ratio < 1 / (1 + tolerance)
        out.append({"metric": key, "label": TRACKED[path][0], "baseline": b, "current": cur[key],
                    "ratio": round(ratio, 3), "regression": bad})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="FIRA monitoring performance benchmark")
    ap.add_argument("--compare", type=Path, default=None, help="baseline JSON: report regressions, exit 1 if any")
    ap.add_argument("--tolerance", type=float, default=0.5, help="allowed slowdown fraction for --compare (default 0.5)")
    ap.add_argument("--sizes", type=int, nargs="+", default=[10_000, 100_000])
    ap.add_argument("--database-url", default=None, help="PostgreSQL server URL (a temporary database is created)")
    ap.add_argument("--batch-rows", type=int, default=5000)
    ap.add_argument("--sample", type=int, default=300)
    ap.add_argument("--out", type=Path, required=True, help="output path without extension")
    a = ap.parse_args()
    import os

    work = Path(tempfile.mkdtemp(prefix="fira-bench-"))
    results = []
    for n in a.sizes:
        print(f"== {n:,} transactions", flush=True)
        results.append(run_size(n, a.database_url, a.batch_rows, a.sample, work))
    res = {"kind": "monitoring_performance", "created_at": datetime.now(timezone.utc).isoformat(), "sizes": a.sizes,
           "database": "PostgreSQL" if a.database_url else None,
           "environment": {"python": platform.python_version(), "platform": platform.platform(),
                           "processor": platform.processor() or platform.machine(), "cpu_count": os.cpu_count()},
           "results": results}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.with_suffix(".json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    md = to_markdown(res)
    a.out.with_suffix(".md").write_text(md, encoding="utf-8")
    print(md)
    if a.compare:
        rows = compare(json.loads(a.compare.read_text(encoding="utf-8")), res, a.tolerance)
        for r in rows:
            print(f"{'REGRESSION' if r['regression'] else 'ok        '} {r['metric']}: {r['baseline']} -> {r['current']} (x{r['ratio']})")
        if any(r["regression"] for r in rows):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
