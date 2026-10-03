"""Automated evaluation runner.

    python -m app.evaluation.runner all     [--per-scenario 10] [--agent-sample 4] [--out PATH]
    python -m app.evaluation.runner risk | retrieval | agent

Results are written to the `evaluation_runs` store (shown on the Evaluation
dashboard) and to evaluation/results/*.json + a Markdown summary.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.data.store import new_id, utcnow
from app.evaluation.benchmarks import (
    REPO,
    agent_benchmark,
    load_labels,
    retrieval_benchmark,
    risk_benchmark,
    sample_labels,
)


def run_all(container: Any, per_scenario: int | None = 10, agent_sample: int = 4, kinds: tuple[str, ...] = (
        "risk", "retrieval", "agent"), persist: bool = True) -> dict[str, Any]:
    labels = load_labels(container.settings, container.store)
    risk_labels = sample_labels(labels, per_scenario)
    out: dict[str, Any] = {"run_id": new_id("EVAL"), "created_at": utcnow().isoformat(),
                           "dataset": str(container.settings.dataset_dir), "config_version": container.risk_config.version}
    if "risk" in kinds:
        r = risk_benchmark(container.risk_engine, risk_labels)
        out["risk"] = {k: v for k, v in r.items() if k != "rows"}
        out["risk_rows"] = r["rows"]
    if "retrieval" in kinds:
        out["retrieval"] = retrieval_benchmark(container.retriever)
    if "agent" in kinds:
        agent_labels = sample_labels(labels, agent_sample, seed=5)
        a = agent_benchmark(container, agent_labels)
        out["agent"] = {k: v for k, v in a.items() if k != "runs"}
        out["agent_runs"] = a["runs"]
    if persist:
        metrics = {k: out[k] for k in ("risk", "retrieval", "agent") if k in out}
        compact = json.loads(json.dumps(metrics, default=str))
        for m in compact.values():
            if isinstance(m, dict):
                for mode in ("keyword", "semantic", "hybrid"):
                    if isinstance(m.get(mode), dict):
                        m[mode].pop("per_query", None)
        container.store.save_evaluation_run({"run_id": out["run_id"], "created_at": utcnow(), "kind": "+".join(kinds),
                                             "config_version": container.risk_config.version, "metrics": compact,
                                             "details": {"dataset": out["dataset"],
                                                         "n_risk_labels": len(risk_labels)}})
    return out


def summary_markdown(res: dict[str, Any]) -> str:
    lines = [f"# FIRA evaluation {res['run_id']}", "", f"- created: {res['created_at']}",
             f"- dataset: {res['dataset']}", f"- risk config: {res['config_version']}", ""]
    if "risk" in res:
        r = res["risk"]
        o = r["overall"]
        lines += ["## Risk detection", "",
                  f"Threshold {r['threshold']}, n={r['n']} labelled subjects, {r['ms_per_assessment']} ms/assessment.", "",
                  "| precision | recall | F1 | FPR | FNR | AUC |", "|---|---|---|---|---|---|",
                  f"| {o['precision']} | {o['recall']} | {o['f1']} | {o['false_positive_rate']} | "
                  f"{o['false_negative_rate']} | {o['auc']} |", "",
                  "| scenario | suspicious | n | flag rate | mean score | expected-signal recall |", "|---|---|---|---|---|---|"]
        for s, v in r["per_scenario"].items():
            lines.append(f"| {s} | {v['suspicious']} | {v['n']} | {v['flag_rate']} | {v['mean_score']} | "
                         f"{v['expected_signal_recall']} |")
        lines += ["", "| signal | fired | precision |", "|---|---|---|"]
        for s, v in r["signals"].items():
            lines.append(f"| {s} | {v['fired']} | {v['precision']} |")
        lines.append("")
    if "retrieval" in res:
        lines += ["## Retrieval", "", "| mode | queries | P@1 | P@3 | R@3 | R@5 | MRR |", "|---|---|---|---|---|---|---|"]
        for mode, v in res["retrieval"].items():
            lines.append(f"| {mode} | {v['n_queries']} | {v.get('p@1')} | {v.get('p@3')} | {v.get('r@3')} | "
                         f"{v.get('r@5')} | {v.get('mrr')} |")
        lines.append("")
    if "agent" in res:
        a = res["agent"]
        lines += ["## Agent / RAG / System", "", f"Engine: {a['engine']}, n={a['n']} investigations, narrative: "
                  f"{a['narrative_sources']}", "", "| metric | value |", "|---|---|"]
        for group in ("agent", "rag", "system"):
            for k, v in a[group].items():
                lines.append(f"| {group}.{k} | {v} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    from app.services.container import build_container

    p = argparse.ArgumentParser()
    p.add_argument("kind", choices=["all", "risk", "retrieval", "agent"])
    p.add_argument("--per-scenario", type=int, default=10, help="labelled subjects per scenario (0 = all)")
    p.add_argument("--agent-sample", type=int, default=4, help="agent runs per scenario")
    p.add_argument("--out", type=Path, default=None)
    a = p.parse_args()
    c = build_container()
    kinds = ("risk", "retrieval", "agent") if a.kind == "all" else (a.kind,)
    res = run_all(c, a.per_scenario or None, a.agent_sample, kinds)
    out = a.out or REPO / "evaluation" / "results" / f"eval_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    md = summary_markdown(res)
    out.with_suffix(".md").write_text(md, encoding="utf-8")
    print(md)
    print(f"written {out}")


if __name__ == "__main__":
    main()
