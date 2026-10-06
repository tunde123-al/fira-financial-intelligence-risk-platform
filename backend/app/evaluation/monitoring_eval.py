"""Evaluation of the transaction-monitoring pipeline (alert generation) on the independent benchmark.

What is measured: given a labelled synthetic bank, a monitoring run over EVERY customer at the dataset end
(30-day lookback, 90-day baseline). A customer is *predicted positive* if the run raised at least one alert
for them. Labels come from the generator (`scenario_labels.json`); customers without a label carry no injected
scenario and are treated as negatives, which is conservative (incidental risky behaviour in the random base
data counts as a false positive).

Nothing here is tuned: thresholds are the shipped defaults (`default_config.yaml`, `monitoring_config.yaml`).
Results describe behaviour on SYNTHETIC data and say nothing about real-world detection performance.

Two further sections cover the production-oriented upgrade:

* triage quality: alerts are ranked by the heuristic triage score; a *simulated investigator* decides each alert from the
  generator's label (alert on a suspicious customer = confirmed, otherwise = false positive). This measures whether the
  ranking puts label-confirmed alerts first. It is an oracle, not a human, and says nothing about real investigators.
* money-mule indicators: scored on every labelled customer plus a random sample of unlabelled ones (assumed benign).
  Here true negatives are defined, so the false-positive rate is valid, but only over that sampled universe.

    python -m app.evaluation.monitoring_eval --customers 4000 --seed 2024 --out ../evaluation/results/monitoring_seed2024
"""
from __future__ import annotations

import argparse
import json
import platform
import random
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from app.monitoring.repository import AlertFilter


def confusion(y_true: dict[str, bool], predicted: set[str]) -> dict[str, int]:
    tp = sum(1 for c, y in y_true.items() if y and c in predicted)
    fp = sum(1 for c, y in y_true.items() if not y and c in predicted)
    fn = sum(1 for c, y in y_true.items() if y and c not in predicted)
    tn = sum(1 for c, y in y_true.items() if not y and c not in predicted)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn}


def rates(cm: dict[str, int]) -> dict[str, float | None]:
    tp, fp, fn, tn = cm["tp"], cm["fp"], cm["fn"], cm["tn"]
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else (0.0 if prec is not None and rec is not None else None)
    fpr = fp / (fp + tn) if fp + tn else None
    return {"precision": prec, "recall": rec, "f1": f1, "false_positive_rate": fpr}


def pr_auc(y_true: dict[str, bool], scores: dict[str, float]) -> float | None:
    """Average precision of ranking customers by their highest alert risk score (0 if no alert)."""
    from sklearn.metrics import average_precision_score

    ids = sorted(y_true)
    y = [int(y_true[c]) for c in ids]
    if not any(y) or all(y):
        return None
    return float(average_precision_score(y, [scores.get(c, 0.0) for c in ids]))


def roc_auc(y_true: dict[str, bool], scores: dict[str, float]) -> float | None:
    from sklearn.metrics import roc_auc_score

    ids = sorted(y_true)
    y = [int(y_true[c]) for c in ids]
    if not any(y) or all(y):
        return None
    return float(roc_auc_score(y, [scores.get(c, 0.0) for c in ids]))


def percentile(values: list[float], q: float) -> float | None:
    return float(np.percentile(values, q)) if values else None


def topk_precision(ranked: list[bool], ks: tuple[int, ...] = (10, 25, 50, 100)) -> dict[str, float | None]:
    return {str(k): (round(sum(ranked[:k]) / min(k, len(ranked)), 4) if len(ranked) >= 1 else None) for k in ks if k <= len(ranked) or k == ks[0]}


def triage_quality(alerts: list[Any], suspicious: set[str], seed: int = 1) -> dict[str, Any]:
    """Does a higher triage priority go with a higher rate of label-confirmed alerts? (oracle dispositions)"""
    from sklearn.metrics import roc_auc_score

    rows = [a for a in alerts if a.triage_score is not None]
    on_susp = [a.customer_id in suspicious for a in rows]
    by_pri: dict[str, dict[str, Any]] = {}
    for a, y in zip(rows, on_susp, strict=True):
        d = by_pri.setdefault(a.triage_priority, {"alerts": 0, "confirmed": 0})
        d["alerts"] += 1
        d["confirmed"] += y
    order = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
    for d in by_pri.values():
        d["confirmed_rate"] = round(d["confirmed"] / d["alerts"], 4)
        d["false_discovery_rate"] = round(1 - d["confirmed"] / d["alerts"], 4)
    present = [k for k in order if k in by_pri]
    rates_ = [by_pri[k]["confirmed_rate"] for k in present]
    ranked_tri = [y for _, y in sorted(zip((a.triage_score for a in rows), on_susp, strict=True), key=lambda t: -t[0])]
    ranked_risk = [y for _, y in sorted(zip((a.risk_score for a in rows), on_susp, strict=True), key=lambda t: -t[0])]
    rng = random.Random(seed)
    shuffled = on_susp[:]
    rng.shuffle(shuffled)
    both = any(on_susp) and not all(on_susp)
    return {
        "oracle": "disposition = label of the alert's customer (suspicious -> confirmed, benign -> false positive)",
        "alerts": len(rows), "overall_confirmed_rate": round(sum(on_susp) / max(len(on_susp), 1), 4),
        "by_priority": {k: by_pri[k] for k in present},
        "monotonic_confirmed_rate": all(a >= b for a, b in zip(rates_, rates_[1:], strict=False)),
        "top_k_precision": {"triage_score": topk_precision(ranked_tri), "customer_risk_score": topk_precision(ranked_risk),
                            "random_order": topk_precision(shuffled)},
        "alert_level_roc_auc": {
            "triage_score": float(roc_auc_score(on_susp, [a.triage_score for a in rows])) if both else None,
            "customer_risk_score": float(roc_auc_score(on_susp, [a.risk_score for a in rows])) if both else None},
    }


def mule_evaluation(c: Any, labels: list[dict[str, Any]], customers: list[str], alerts: list[Any], sample: int,
                    seed: int) -> dict[str, Any]:
    from sklearn.metrics import roc_auc_score

    from app.synthetic.monitoring_benchmark import MULE_TYPOLOGIES

    scenario_of = {lb["entity_id"]: lb["scenario"] for lb in labels}
    unlabelled = [x for x in customers if x not in scenario_of]
    rng = random.Random(seed)
    universe = sorted(scenario_of) + sorted(rng.sample(unlabelled, min(sample, len(unlabelled))))
    truth = {cid: scenario_of.get(cid) in MULE_TYPOLOGIES for cid in universe}
    results = {cid: c.monitoring.mule_assessment(cid) for cid in universe}
    flow_alert = {a.customer_id for a in alerts if a.detector_id in ("FAN_IN", "RAPID_PASS_THROUGH")}

    def at(pred: set[str]) -> dict[str, Any]:
        cm = confusion(truth, pred)
        return {**cm, **rates(cm)}

    medium_up = {cid for cid, r in results.items() if r["band"] in ("MEDIUM", "HIGH")}
    high = {cid for cid, r in results.items() if r["band"] == "HIGH"}
    per: dict[str, dict[str, Any]] = {}
    for cid, r in results.items():
        sc = scenario_of.get(cid, "(unlabelled sample, assumed benign)")
        d = per.setdefault(sc, {"mule_typology": sc in MULE_TYPOLOGIES, "n": 0, "bands": {}, "fired": {}})
        d["n"] += 1
        d["bands"][r["band"]] = d["bands"].get(r["band"], 0) + 1
        for i in r["indicators"]:
            if i["fired"]:
                d["fired"][i["id"]] = d["fired"].get(i["id"], 0) + 1
    both = any(truth.values()) and not all(truth.values())
    return {
        "universe": {"labelled_customers": len(scenario_of), "unlabelled_sample": len(universe) - len(scenario_of),
                     "mule_typology_customers": sum(truth.values()), "typologies": sorted(MULE_TYPOLOGIES)},
        "note": "FPR is valid here because every customer in the universe has a label or is an assumed-benign random sample",
        "medium_or_high": at(medium_up), "high_only": at(high),
        "baseline_fan_in_or_rapid_alert": at(flow_alert & set(universe)),
        "roc_auc_score": float(roc_auc_score([truth[c_] for c_ in universe], [results[c_]["score"] for c_ in universe])) if both else None,
        "per_scenario": dict(sorted(per.items())),
        "thresholds": c.monitoring_config.mule.model_dump(),
    }


def evaluate(dataset_dir: Path, latency_sample: int = 300, seed: int = 1,
             supporting_min_score: float | None = None, mule_sample: int = 400) -> dict[str, Any]:
    from app.config import Settings
    from app.services.container import build_container

    model_dir = Path(tempfile.mkdtemp(prefix="fira-eval-models-"))
    settings = Settings(environment="test", data_backend="frames", dataset_dir=dataset_dir, model_dir=model_dir,
                        graph_backend="networkx", vector_backend="memory", llm_provider="none",
                        tool_timeout_s=60.0)
    t_build = time.perf_counter()
    c = build_container(settings, load_ml=False)
    build_s = time.perf_counter() - t_build
    if supporting_min_score is not None:  # ablation: 0 = every triggered supporting detector alerts
        c.monitoring_config.alerting.supporting_min_customer_score = supporting_min_score
    labels = json.loads((dataset_dir / "scenario_labels.json").read_text(encoding="utf-8"))
    scenario_of = {lb["entity_id"]: lb["scenario"] for lb in labels}
    suspicious = {lb["entity_id"] for lb in labels if lb["is_suspicious"]}
    customers = c.store.list_customer_ids()
    y_true = {cid: cid in suspicious for cid in customers}

    end = c.store.as_of()
    lookback = c.monitoring_config.lookback_days
    t0 = time.perf_counter()
    run = c.monitoring.run(window_end=end, customer_ids=customers, mode="evaluation", max_customers=len(customers) + 1)
    run_s = time.perf_counter() - t0
    alerts, _ = c.monitoring_repo.list_alerts(AlertFilter(limit=10**7))
    window_txns = c.store.count_transactions(end - timedelta(days=lookback), end)
    total_txns = c.store.count_transactions(None, None)

    predicted = {a.customer_id for a in alerts}
    scores: dict[str, float] = {}
    for a in alerts:
        scores[a.customer_id] = max(scores.get(a.customer_id, 0.0), a.risk_score)
    cm = confusion(y_true, predicted)
    overall = {**cm, **rates(cm), "pr_auc": pr_auc(y_true, scores), "roc_auc": roc_auc(y_true, scores),
               "prevalence": sum(y_true.values()) / max(len(y_true), 1)}

    # operating points on the risk score of the customer's highest alert (descriptive, not tuned)
    sweep = []
    for cut in (0, 20, 30, 40, 50, 60):
        pred = {cid for cid, s in scores.items() if s >= cut}
        cmx = confusion(y_true, pred)
        sweep.append({"min_alert_risk_score": cut, **cmx, **rates(cmx)})

    per_scenario: dict[str, dict[str, Any]] = {}
    for cid in customers:
        sc = scenario_of.get(cid, "(unlabelled, assumed benign)")
        d = per_scenario.setdefault(sc, {"suspicious": cid in suspicious, "n": 0, "alerted": 0})
        d["n"] += 1
        d["alerted"] += cid in predicted
    for d in per_scenario.values():
        d["alert_rate"] = round(d["alerted"] / d["n"], 4)

    expected_hit: dict[str, dict[str, Any]] = {}
    by_cust: dict[str, set[str]] = {}
    for a in alerts:
        by_cust.setdefault(a.customer_id, set()).add(a.detector_id)
    for lb in labels:
        if lb["is_suspicious"] and lb["expected_signals"]:
            d = expected_hit.setdefault(lb["scenario"], {"n": 0, "expected_detector_alerted": 0})
            d["n"] += 1
            d["expected_detector_alerted"] += bool(set(lb["expected_signals"]) & by_cust.get(lb["entity_id"], set()))
    for d in expected_hit.values():
        d["rate"] = round(d["expected_detector_alerted"] / d["n"], 4)

    per_detector: dict[str, dict[str, Any]] = {}
    for a in alerts:
        d = per_detector.setdefault(a.detector_id, {"alerts": 0, "on_suspicious_customers": 0})
        d["alerts"] += 1
        d["on_suspicious_customers"] += a.customer_id in suspicious
    for d in per_detector.values():
        d["alert_precision"] = round(d["on_suspicious_customers"] / d["alerts"], 4)

    # detection latency: wall-clock per customer (assessment + detector results), sampled
    rng = random.Random(seed)
    sample = rng.sample(customers, min(latency_sample, len(customers)))
    from app.monitoring.detectors import detector_results

    lat_ms: list[float] = []
    for cid in sample:
        t = time.perf_counter()
        a = c.risk_engine.assess_customer(cid, end, lookback, c.monitoring_config.baseline_days)
        detector_results(a, include_not_triggered=False)
        lat_ms.append((time.perf_counter() - t) * 1000)

    sev: dict[str, int] = {}
    for a in alerts:
        sev[a.severity] = sev.get(a.severity, 0) + 1
    manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    triage = triage_quality(alerts, suspicious, seed)
    t_m = time.perf_counter()
    mule = mule_evaluation(c, labels, customers, alerts, mule_sample, seed)
    mule["assessment_seconds"] = round(time.perf_counter() - t_m, 2)
    return {
        "kind": "monitoring_evaluation",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {"generator": manifest.get("generator"), "seed": manifest.get("seed"),
                    "customers": len(customers), "transactions": total_txns, "synthetic": True,
                    "labelled_suspicious": len(suspicious), "labelled_benign": sum(1 for lb in labels if not lb["is_suspicious"]),
                    "scenarios": manifest.get("scenarios")},
        "configuration": {"risk_config": c.risk_config.version, "risk_config_fingerprint": c.risk_config.fingerprint(),
                          "monitoring_config": c.monitoring_config.version,
                          "monitoring_fingerprint": c.monitoring_config.fingerprint(), "lookback_days": lookback,
                          "baseline_days": c.monitoring_config.baseline_days, "window_end": end.isoformat(),
                          "thresholds_tuned_on_this_data": False,
                          "supporting_min_customer_score": c.monitoring_config.alerting.supporting_min_customer_score},
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "processor": platform.processor() or platform.machine()},
        "overall": overall,
        "operating_points": sweep,
        "triage_quality": triage,
        "money_mule": mule,
        "per_scenario": dict(sorted(per_scenario.items())),
        "expected_detector_recall": expected_hit,
        "per_detector": dict(sorted(per_detector.items())),
        "volumes": {
            "alerts": len(alerts), "alerting_customers": len(predicted), "severity": sev,
            "transactions_in_window": window_txns,
            "alerts_per_1000_window_transactions": round(len(alerts) / max(window_txns, 1) * 1000, 3),
            "alerts_per_1000_customers": round(len(alerts) / max(len(customers), 1) * 1000, 2),
            "run_errors": run.errors,
        },
        "performance": {
            "container_build_s": round(build_s, 2), "monitoring_run_s": round(run_s, 2),
            "customers_per_second": round(len(customers) / run_s, 2),
            "window_transactions_per_second": round(window_txns / run_s, 1),
            "per_customer_ms_mean_in_run": round(run.duration_ms / max(run.customers_evaluated, 1), 2),
            "latency_sample_size": len(lat_ms),
            "detection_latency_ms": {"mean": round(float(np.mean(lat_ms)), 2), "p50": round(percentile(lat_ms, 50) or 0, 2),
                                     "p95": round(percentile(lat_ms, 95) or 0, 2), "p99": round(percentile(lat_ms, 99) or 0, 2)},
        },
    }


def _f(v: Any, nd: int = 4) -> str:
    return "n/a" if v is None else f"{v:.{nd}f}"


def to_markdown(r: dict[str, Any]) -> str:
    o, ds, cfg, vol, perf = r["overall"], r["dataset"], r["configuration"], r["volumes"], r["performance"]
    lines = [
        f"# FIRA transaction-monitoring evaluation ({r['created_at'][:10]})", "",
        "SYNTHETIC data only. These numbers describe how the pipeline behaves on a generated bank whose scenarios were "
        "written by the same authors as the detectors. They are not evidence of real-world AML or fraud detection "
        "performance.", "",
        f"- dataset: {ds['generator']}, seed {ds['seed']}, {ds['customers']:,} customers, {ds['transactions']:,} transactions, "
        f"{ds['labelled_suspicious']} suspicious customers (prevalence {o['prevalence']:.3%})",
        f"- configuration: risk `{cfg['risk_config']}` ({cfg['risk_config_fingerprint']}), monitoring `{cfg['monitoring_config']}` "
        f"({cfg['monitoring_fingerprint']}), lookback {cfg['lookback_days']} d, baseline {cfg['baseline_days']} d, "
        f"window end {cfg['window_end'][:10]}",
        f"- thresholds tuned on this data: **{cfg['thresholds_tuned_on_this_data']}**; supporting-detector gate: "
        f"{cfg['supporting_min_customer_score'] if cfg['supporting_min_customer_score'] is not None else 'default (investigation threshold)'}",
        f"- environment: Python {r['environment']['python']}, {r['environment']['platform']}", "",
        "## Customer-level detection (a customer is positive if any alert was raised)", "",
        "| precision | recall | F1 | FPR | PR-AUC | ROC-AUC | TP | FP | FN | TN |", "|---|---|---|---|---|---|---|---|---|---|",
        f"| {_f(o['precision'])} | {_f(o['recall'])} | {_f(o['f1'])} | {_f(o['false_positive_rate'])} | {_f(o['pr_auc'])} | "
        f"{_f(o['roc_auc'])} | {o['tp']} | {o['fp']} | {o['fn']} | {o['tn']} |", "",
        f"PR-AUC ranks customers by their highest alert risk score (no alert = 0). The no-skill PR-AUC equals the "
        f"prevalence, {o['prevalence']:.4f}.", "",
        "### Operating points (descriptive; the shipped configuration alerts at any score)", "",
        "| min alert risk score | precision | recall | F1 | FPR | TP | FP | FN |", "|---|---|---|---|---|---|---|---|",
    ]
    for s in r["operating_points"]:
        lines.append(f"| {s['min_alert_risk_score']} | {_f(s['precision'])} | {_f(s['recall'])} | {_f(s['f1'])} | "
                     f"{_f(s['false_positive_rate'])} | {s['tp']} | {s['fp']} | {s['fn']} |")
    lines += ["", "## Per scenario", "", "| scenario | suspicious | customers | alerted | alert rate |", "|---|---|---|---|---|"]
    for k, v in r["per_scenario"].items():
        lines.append(f"| {k} | {v['suspicious']} | {v['n']} | {v['alerted']} | {v['alert_rate']:.3f} |")
    lines += ["", "For suspicious scenarios the alert rate is recall; for benign ones it is the false-positive rate.", "",
              "### Did the *expected* detector fire?", "", "| scenario | customers | expected detector alerted | rate |", "|---|---|---|---|"]
    for k, v in sorted(r["expected_detector_recall"].items()):
        lines.append(f"| {k} | {v['n']} | {v['expected_detector_alerted']} | {v['rate']:.3f} |")
    lines += ["", "## Per detector (alerts raised)", "", "| detector | alerts | on suspicious customers | alert precision |", "|---|---|---|---|"]
    for k, v in r["per_detector"].items():
        lines.append(f"| {k} | {v['alerts']} | {v['on_suspicious_customers']} | {v['alert_precision']:.3f} |")
    tq, mm = r["triage_quality"], r["money_mule"]
    lines += ["", "## Triage quality (oracle dispositions from the labels)", "", tq["oracle"] + ".", "",
              f"{tq['alerts']} alerts, overall confirmed rate {tq['overall_confirmed_rate']:.3f}. "
              f"Confirmed rate falls monotonically with priority: **{tq['monotonic_confirmed_rate']}**.", "",
              "| priority | alerts | confirmed | confirmed rate | false-discovery rate |", "|---|---|---|---|---|"]
    for k, v in tq["by_priority"].items():
        lines.append(f"| {k} | {v['alerts']} | {v['confirmed']} | {v['confirmed_rate']:.3f} | {v['false_discovery_rate']:.3f} |")
    lines += ["", "Top-k precision (share of the k highest-ranked alerts that the oracle confirms):", "",
              "| ranking | " + " | ".join(f"top {k}" for k in tq["top_k_precision"]["triage_score"]) + " |",
              "|---|" + "---|" * len(tq["top_k_precision"]["triage_score"])]
    for name, d in tq["top_k_precision"].items():
        lines.append(f"| {name} | " + " | ".join(_f(v, 3) for v in d.values()) + " |")
    lines += ["", f"Alert-level ROC-AUC: triage score {_f(tq['alert_level_roc_auc']['triage_score'])}, "
                  f"customer risk score {_f(tq['alert_level_roc_auc']['customer_risk_score'])}.", "",
              "## Money-mule indicators", "",
              f"Universe: {mm['universe']['labelled_customers']} labelled customers + {mm['universe']['unlabelled_sample']} "
              f"sampled unlabelled (assumed benign); {mm['universe']['mule_typology_customers']} are mule typologies "
              f"({', '.join(mm['universe']['typologies'])}). {mm['note']}.", "",
              "| rule | precision | recall | F1 | FPR | TP | FP | FN | TN |", "|---|---|---|---|---|---|---|---|---|"]
    for name, key in (("mule band MEDIUM or HIGH", "medium_or_high"), ("mule band HIGH", "high_only"),
                      ("baseline: FAN_IN or RAPID_PASS_THROUGH alert", "baseline_fan_in_or_rapid_alert")):
        v = mm[key]
        lines.append(f"| {name} | {_f(v['precision'])} | {_f(v['recall'])} | {_f(v['f1'])} | {_f(v['false_positive_rate'])} | "
                     f"{v['tp']} | {v['fp']} | {v['fn']} | {v['tn']} |")
    lines += ["", f"ROC-AUC of the mule score: {_f(mm['roc_auc_score'])}.", "",
              "| scenario | mule typology | customers | band counts | indicators that fired |", "|---|---|---|---|---|"]
    for k, v in mm["per_scenario"].items():
        lines.append(f"| {k} | {v['mule_typology']} | {v['n']} | {v['bands']} | {v['fired']} |")
    lines += [
        "", "## Alert volume", "",
        f"- alerts: {vol['alerts']:,} on {vol['alerting_customers']:,} customers; severity {vol['severity']}",
        f"- **alerts per 1,000 transactions** (transactions in the {cfg['lookback_days']}-day window, {vol['transactions_in_window']:,}): "
        f"{vol['alerts_per_1000_window_transactions']}",
        f"- alerts per 1,000 customers: {vol['alerts_per_1000_customers']}", "",
        "## Speed on this machine (single process, in-memory store)", "",
        f"- monitoring run over all {ds['customers']:,} customers: {perf['monitoring_run_s']} s "
        f"({perf['customers_per_second']} customers/s, {perf['window_transactions_per_second']} window transactions/s)",
        f"- detection latency per customer (assessment + detector results, n={perf['latency_sample_size']}): "
        f"mean {perf['detection_latency_ms']['mean']} ms, p50 {perf['detection_latency_ms']['p50']} ms, "
        f"p95 {perf['detection_latency_ms']['p95']} ms, p99 {perf['detection_latency_ms']['p99']} ms", "",
        "Latency here is processing time. The benchmark runs in batch mode, so dataset-time lag between a transaction "
        "and its alert is not measured.", "",
        "## How to reproduce", "", "```bash", "cd backend",
        f"python -m app.synthetic.monitoring_benchmark --out ../data/benchmark --customers {ds['customers']} --seed {ds['seed']}",
        "python -m app.evaluation.monitoring_eval --dataset ../data/benchmark --out ../evaluation/results/monitoring", "```", "",
        "Counts and rates are deterministic for a given seed, configuration and library versions. Timings vary by machine.", ""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Evaluate the monitoring pipeline on the monitoring benchmark")
    ap.add_argument("--dataset", type=Path, default=None, help="existing benchmark dataset (else generate one)")
    ap.add_argument("--customers", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=2024)
    ap.add_argument("--latency-sample", type=int, default=300)
    ap.add_argument("--supporting-min-score", type=float, default=None,
                    help="ablation: gate for supporting-tier detectors (0 = alert on every triggered detector)")
    ap.add_argument("--mule-sample", type=int, default=400, help="unlabelled customers sampled for the mule evaluation")
    ap.add_argument("--out", type=Path, required=True, help="output path without extension (.json and .md are written)")
    a = ap.parse_args()
    dataset = a.dataset
    if dataset is None:
        from app.synthetic.monitoring_benchmark import generate_monitoring_benchmark

        dataset = Path(tempfile.mkdtemp(prefix="fira-benchmark-"))
        generate_monitoring_benchmark(dataset, a.customers, a.seed)
    res = evaluate(dataset, a.latency_sample, supporting_min_score=a.supporting_min_score, mule_sample=a.mule_sample)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.with_suffix(".json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    md = to_markdown(res)
    a.out.with_suffix(".md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
