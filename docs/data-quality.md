# Data quality

FIRA has two data-quality layers. Both compute their figures from the data; nothing is hard-coded.

| Layer | Question it answers | Where |
|---|---|---|
| **Ingestion gate** | "What happened to the rows we were just sent?" | `app/monitoring/ingest.py`; `GET /api/data-quality/summary`, `/batches`, `/rejected` |
| **Stored-dataset audit** | "What is wrong with the data we hold?" | `app/data/quality.py`; `GET /api/data-quality/dataset` |

## Stored-dataset audit

One pandas implementation serves both stores (the PostgreSQL store reads only the columns it needs; the result is identical, covered by a
parity test).

**Transaction checks** (each row can trip several; a transaction is *valid* if it trips none):

| Check | Rule |
|---|---|
| missing_transaction_id | id empty |
| duplicate_transaction_id | an id seen before (every repeat counted) |
| likely_duplicate | same sender, receiver, amount, currency within 60 s of the previous such row |
| invalid_amount | missing, not finite, or <= 0 |
| invalid_currency | not a configured currency |
| invalid_timestamp / future_timestamp | missing/unparseable; more than one day after the dataset's as-of date |
| no_account | neither sender nor receiver |
| orphan_sender_account / orphan_receiver_account / orphan_merchant / orphan_device | referenced id does not exist |
| impossible_state | status outside {completed, failed, reversed, pending}, or a type that needs a side the row lacks (deposit without receiver, ...) |
| incomplete_record | transaction type or channel missing |

Other tables: duplicate customer ids, incomplete customers (country/segment), orphan account -> customer references, missing account status.
Freshness: dataset as-of date, latest transaction, dataset age (a frozen synthetic snapshot is expected to be old; a live feed would alert).

**Score:** `quality_score = 100 x valid / records processed`; `null` (not 100) when there are no transactions. Example output shape:

```text
Records processed: <count from the table>     Valid: <processed - with issues>
Duplicates: <duplicate ids + likely duplicates>   Invalid amounts: <n>   Missing ids: <n>   Orphans: <sum of orphan checks>
Quality score: <computed>
```

On the generated default dataset every check is 0 and the score is 100: the generator produces clean data. **The checks are proven by tests that inject
faults** (`tests/unit/test_dataset_quality.py`: one assertion per check, plus likely-duplicate edge cases and the empty table), and by a test that
seeds an orphan + duplicate id into the live store and sees the counts move.

Surfaces: the Data Quality page ("Stored dataset audit"), a dashboard tile, and the API. Cost: about 0.3 s on 15,000 transactions; it reads
the whole transaction table (acceptable at the 250,000 transactions measured; not designed for far larger tables).

## Ingestion gate (summary)

22 reason codes in four groups, batch accounting (`received = processed + rejected + failed`, enforced by a database CHECK), quarantine with
sanitised payloads, coverage only against a *declared* expectation, late and missing counts. Details: [PRODUCTION_ORIENTED_ARCHITECTURE.md](PRODUCTION_ORIENTED_ARCHITECTURE.md)
section 14 and [DATA_MODEL.md](DATA_MODEL.md).

## Limitations

The audit does not look at semantic plausibility (a valid-looking transaction can still be unusual: that is the risk engine's job), does
not reconcile against an external source of truth, and does not monitor drift over time. No remediation is automated: findings are reported for a person.
