# Evaluation

```bash
make evaluate-local                     # in-process: risk + retrieval + agent
python -m app.evaluation.runner risk    # or retrieval | agent | all  [--per-scenario N] [--agent-sample N]
```

Each run is stored in `evaluation_runs` (shown on the UI's Evaluation page) and written to
`evaluation/results/*.json` with a Markdown summary. Admins can also trigger a run from the UI or
with `POST /api/evaluation/run`.

## Benchmarks and metrics

| Benchmark | Data | Metrics |
|---|---|---|
| Risk detection | every labelled subject (or N per scenario): 8 suspicious typologies, 3 false-positive traps, 300 normal customers | precision, recall, F1, FPR, FNR, ROC-AUC; per-scenario flag rate and **expected-signal recall**; per-signal precision |
| Retrieval | 25 hand-labelled queries (`evaluation/retrieval_qrels.json`; a chunk is relevant if document and section match) | P@1/3/5, R@1/3/5, MRR — for keyword, semantic and hybrid |
| Agent | full agent runs on sampled labelled subjects | tool-selection recall and precision against the plan, task completion (status plus all report sections), evidence coverage (expected signals present in the report), unsupported-claim rate |
| RAG | the same runs | context relevance (retrieved passages relevant per qrels to the signal they were retrieved for), groundedness (1 − unsupported rate), citation correctness (cited evidence shares content with the claim) |
| System | the same runs | latency p50/p95, token usage, cost, tool-call count, tool-failure rate, timeout rate |

## Results

**Dev dataset** (seed 42, 10,000 customers, all 631 labelled subjects; ML model enabled):

| precision | recall | F1 | FPR | FNR | AUC |
|---|---|---|---|---|---|
| 0.991 | 0.890 | 0.938 | 0.005 | 0.110 | 0.998 |

**Holdout dataset** (seed 7, 5,000 customers, 467 labelled subjects, never used for tuning):

| precision | recall | F1 | FPR | FNR | AUC |
|---|---|---|---|---|---|
| 0.991 | 0.912 | 0.950 | 0.003 | 0.088 | 0.999 |

**Population alert rate** (1,000 random dev customers): 2.5% flagged overall, 0.10% of customers
without an injected suspicious scenario.

Per scenario (dev): circular transfers, impossible travel and mule accounts are caught 100% of the
time. Account takeover 93%, dormant reactivation 84%, device rings 83%, high-risk merchant 80% and
card-testing bursts 70% are the weakest; misses are scenarios where only one moderate signal fired.
False-positive traps: high-frequency legitimate businesses 0%, legitimate one-off high-value
payments 0%, legitimate travel 8% (new country plus large foreign card spend).

Agent (42 runs, deterministic narrative): tool-selection recall 1.00, precision 0.99, task
completion 1.00, evidence coverage 0.98, unsupported-claim rate 0.00, context relevance 0.77,
citation correctness 0.98, latency p50 0.5 s and p95 0.8 s (in-process stores), 0 tool failures
and 0 timeouts.

Retrieval: see RAG_ARCHITECTURE.md (hybrid R@5 0.95, MRR 0.95).

## How to read these numbers — caveats

1. **Synthetic, co-designed data.** The detectors and the generator were written by the same team,
   and the injected typologies are clean. High scores show that the pipeline is wired correctly
   end to end and that the scoring design separates the scenarios it was designed for. They say
   **nothing** about performance on real customers, where typologies are noisier and labels are
   incomplete.
2. **Tuning leakage.** Three weights were adjusted after the first dev-set run. The holdout uses a
   different seed but the same generator, so it guards against seed-specific overfitting only.
3. **ML precision 1.0** on labelled subjects reflects the evaluation sample (37% suspicious), not
   population precision.
4. **Agent metrics with the deterministic narrative** measure the evidence pipeline and validator.
   With an LLM configured, the same benchmark measures LLM groundedness; the validator removes
   unsupported claims, and their rate is reported.
5. The **retrieval qrels** were written by the corpus author.

### Evaluating on real data

Load real or anonymised data with `app/db/loader.py` (see DATA_MODEL.md) and labelled outcomes into
`scenario_labels` (entity, scenario/typology, `is_suspicious`, `expected_signals` if known). The
runner then reports the same metrics. Use a time-based split (tune on older periods, test on newer
ones), and track alert volume per signal alongside precision and recall.

## Regression gate for configuration changes

`app/evaluation/improvement.py::regress` compares a candidate configuration with the active one on
the labelled set. A candidate is accepted only if recall drops by at most 0.02, FPR rises by at
most 0.01, and F1 does not get worse. `tests/e2e` checks both directions: a mild change passes,
and disabling the strongest detectors is rejected.

## Tests

| Suite | Count | Covers |
|---|---|---|
| `tests/unit` | 63 | metrics, geo, statistics, scoring and group caps, every detector against ground truth, graph algorithms, parsers (MD/PDF/OCR/DOCX), chunk provenance, BM25/LSA/vector store, retrieval quality floor, validator, auth/JWT/masking/rate limits/roles, tool registry error envelopes, generator integrity and determinism |
| `tests/agent` | 11 | tool selection per subject type, lookback parsing, missing subject, unknown entity, unsupported merchant, malformed tool output, conflicting history, hallucinated LLM claims removed, retry then fallback, LLM errors, episode content, LangGraph parity (CI) |
| `tests/e2e` | 2 | the full acceptance workflow; controlled improvement with four-eyes approval |
| `tests/integration` | 8 | PostgreSQL load and reads equal the in-process store; risk engine identical on both stores; write round-trips; Neo4j vs NetworkX parity; Qdrant; MCP; HTTP acceptance flow |
