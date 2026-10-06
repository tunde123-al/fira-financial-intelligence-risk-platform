# Alert quality: what FIRA measures, what it does not, and why

Everything here is **synthetic data and a prototype**. The measurements describe how this software behaves on a generated bank;
they are not evidence of real-world fraud or AML performance, and the alert workflow has never been exercised by real
investigators.

## 1. The measurement problem

An alert system produces a stream of alerts. A human decides some of them. That gives you outcomes for *decided alerts* and
nothing else:

| quantity | known in operation? | why |
|---|---|---|
| decided alerts: confirmed / cleared / false positive | yes | an investigator recorded the decision |
| undecided alerts | no | no outcome yet; counting them as "false" or "true" would be invention |
| customers who never alerted | **no** | nobody verified them; they are not "true negatives", they are *unknown* |
| suspicious customers who never alerted | **no** | that is exactly what a monitoring system cannot see |

So two textbook metrics are **invalid in operation**:

* **False-positive rate** `FP / (FP + TN)` needs verified true negatives. There are none.
* **Recall** `TP / (TP + FN)` needs the count of missed suspicious customers. It is unknowable.

FIRA therefore does **not** compute FPR or recall from the alert workflow, anywhere: not in the API, not in the dashboard, not
in the metrics endpoint. `GET /api/monitoring/quality` returns a `not_computed` object that says so and why. (An earlier
version of the dashboard showed "false-positive rate" for what was really a share of resolved alerts; that was wrong, it was
renamed, and this document records the correction.)

FPR, recall, F1 and precision *do* appear in the labelled synthetic evaluation ([EVALUATION.md](EVALUATION.md)), where every
customer's truth is known by construction and true negatives are defined.

## 2. What is measured operationally

Computed from stored alerts only (`backend/app/monitoring/quality.py`):

| metric | definition | denominator |
|---|---|---|
| decided | alerts with status RESOLVED and a recorded resolution | all alerts in the window |
| **confirmation rate** | confirmed suspicious / decided | decided alerts |
| **false-discovery rate** | (cleared + false positive) / decided = 1 − confirmation rate | decided alerts |
| **closure rate** | decided / all alerts | all alerts in the window |
| open backlog | alerts not yet decided | |
| time to decision | median and max of (resolved_at − triggered_at), hours | decided alerts |
| per priority / detector / severity | the same figures per group | the group's alerts |

Rules the code follows:

* A rate whose denominator is zero is `null`, never `0`.
* A group with fewer than 30 decided alerts carries `low_sample: true`; the dashboard says "indicative only".
* The window is by *trigger* time (`date_from`), so a still-open alert never silently falls out.
* "Escalated" is a status, not an outcome of closure. The count of alerts currently escalated is reported separately
  (`escalated_now`).

## 3. Outcome feedback

Every decision is recorded with the alert, the decision, the reason, the investigator and the timestamp
(`resolution`, `resolution_reason`, `resolved_by`, `resolved_at` on the alert, plus an append-only `alert_events` row and an
audit-log row). `GET /api/monitoring/feedback` lists them, newest first.

Allowed outcomes: `FALSE_POSITIVE`, `CLEARED`, `CONFIRMED_SUSPICIOUS` (resolutions), and `ESCALATED` (a status transition that
also requires a written reason of at least five characters). Resolving without a resolution and a reason is refused with 409.

**No automatic learning.** One decision does not change any threshold, weight or model. The only inputs a past decision has
on a future alert are two *documented triage factors* (below), and only decisions on **earlier, already-closed alerts of the same
customer** are used, never the alert's own outcome. Changing a threshold is a deliberate, reviewed act and is written to the
configuration change log ([SECURITY.md](SECURITY.md#configuration-governance)).

## 4. Triage: ordering the work

A deterministic score from 0 to 100 helps an investigator decide what to open first. It is **a ranking aid, not a probability**:
nothing in it estimates the chance that a customer is a criminal, and the UI says so.

Twelve factors, each with a fixed maximum. The maxima add up to exactly 100 (asserted by a unit test):

| factor | max | what it reads |
|---|---|---|
| customer risk score | 25 | the risk engine's 0-100 score for the customer |
| detector severity | 13 | severity of the detector that fired |
| value of supporting transactions | 10 | USD total, tiered (config) |
| corroborating signal categories | 10 | distinct risk categories scoring for the customer |
| network exposure | 7 | the engine's network-exposure signal |
| detector tier | 5 | standalone typology vs supporting deviation |
| number of supporting transactions | 5 | |
| pattern repetition | 5 | times the alert recurred while open |
| device / identity signals | 5 | |
| customer's earlier alerts confirmed | 5 | resolved alerts of this customer, excluding this one |
| no earlier clearance of this detector | 5 | resolved alerts of this customer for this detector |
| age of the unresolved alert | 5 | hours open (older work rises); resolved alerts keep their score |

Bands (configurable, `monitoring_config.yaml: triage`): **CRITICAL ≥ 65, HIGH ≥ 50, MEDIUM ≥ 30, LOW below**. Overdue targets used
only for "overdue" flags (assumptions, not regulatory SLAs): CRITICAL 4 h, HIGH 24 h, MEDIUM 72 h, LOW 168 h.

Every factor is stored with the alert (`triage_factors`: label, points, maximum, observed value, one-sentence reason) so the
screen can show exactly why an alert ranks where it does (`GET /api/monitoring/triage/{alert_id}`). Priority changes produce an
alert event and an audit row. Re-scoring (`POST /api/monitoring/triage/recompute`, admin) is how age and new outcomes are picked up.

### How the weights and thresholds were chosen (and the first design that failed)

* The first design (customer score 15, detector tier 10, thresholds 75/55/35) was run on the development benchmark (seed 2025).
  Measured against the oracle dispositions below it **failed**: confirmed rate was not monotonic in priority (HIGH 0.92,
  MEDIUM 0.87, LOW 1.00 on n=7), no alert reached CRITICAL, and the alert-level ROC-AUC of the triage score was 0.65 against 0.87
  for the customer risk score alone. The cause was a design error: supporting-tier alerts exist *because* the customer's score is
  already high, so penalising their tier mis-ranked them. (Result file kept:
  `evaluation/results/monitoring_v3_dev_seed2025_first_design.md`.)
* The weights were then revised once, on that same development data (customer score 25, tier 5), and the thresholds were moved to
  65/50/30 after looking at the score distribution so that every band is reachable (the highest score was 70).
* The **holdout** benchmark (seed 7) was generated and run only after that, with nothing changed. Numbers below are from it.
  The development numbers are published beside it, with the caveat that the design was tuned on them.

## 5. Does priority track outcome? (synthetic, oracle dispositions)

The oracle is the generator's label for the alert's customer (suspicious → confirmed, benign → false positive). It models a
perfect, label-following investigator. It measures whether the *ranking* puts label-confirmed alerts first. It says nothing
about real investigators, and the alert population is 92% label-confirmed, which leaves little room to discriminate.

Holdout benchmark (seed 7, 4,000 customers, 463 alerts):

| priority | alerts | confirmed | confirmed rate | false-discovery rate |
|---|---|---|---|---|
| CRITICAL | 18 | 18 | 1.000 | 0.000 |
| HIGH | 274 | 269 | 0.982 | 0.018 |
| MEDIUM | 169 | 138 | 0.817 | 0.183 |
| LOW | 2 | 0 | 0.000 | 1.000 |

The confirmed rate falls monotonically with priority (CRITICAL ≥ HIGH ≥ MEDIUM ≥ LOW), so higher-priority alerts do have a higher
oracle-confirmed rate. LOW has two alerts, which is far too few to say anything about.

**What the triage score does not do:** as a pure ranking of alerts it is *not better* than the customer risk score it mostly
contains: alert-level ROC-AUC **0.83 vs 0.92**; top-10/25/50/100 precision is 1.0 for both (random order: 1.0 / 0.92 / 0.92 / 0.90).
Its value is the decomposition into reviewable factors and the workflow features (history, age, repetition), not extra accuracy.

## 6. Reading the numbers responsibly

* A high confirmation rate means investigators agreed with alerts, not that the alerts found crime.
* A low false-discovery rate on few decided alerts is noise.
* Alerts on benign look-alikes (marketplace sellers, family collections, rent splits, first payrolls) are *expected* in the
  benchmark and are counted as false positives by design.
* Suppressing noisy detectors is a decision for a person; use the detector switch (logged) and look at the effect on the
  confirmation rate afterwards.

## 7. Endpoints and screens

| | |
|---|---|
| `GET /api/monitoring/quality?date_from=` | rates above, by priority/detector/severity, definitions, `not_computed` |
| `GET /api/monitoring/feedback` | recorded decisions: alert, decision, reason, investigator, time |
| `GET /api/monitoring/triage/{alert_id}` | the factor breakdown |
| `POST /api/monitoring/triage/recompute` | admin; re-score unresolved alerts |
| `GET /api/monitoring/my-work` | the caller's queue, overdue items, recent outcomes |
| UI: Operations, Alert Queue (priority filter and sort), Alert detail (triage card), My Work | |
