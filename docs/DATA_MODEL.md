# Data model

The DDL lives in `backend/app/db/schema.sql`, which is the single source of truth. Alembic revision
`0001` applies it, then applies `schema_postgis.sql` when the PostGIS extension is available.

## Core banking entities

| Table | Key columns | Notes |
|---|---|---|
| `customers` | `customer_id` (CUST-n), `customer_type` (individual/business), `segment`, `created_at`, `country`, `home_city`, `home_latitude/longitude`, `risk_profile`, `status`, `kyc_level` | No names are stored |
| `customer_identifiers` | `customer_id`, `identifier_type` (phone/email/address), `value_hash` (salted SHA-256), `masked_value` | **PII minimisation.** Matching uses hashes, display uses masks. Index on (type, hash) for shared-identifier queries |
| `accounts` | `account_id`, `customer_id`, `account_type`, `currency`, `opened_at`, `status`, `balance` | |
| `merchants` | `merchant_id`, `name`, `category`, `country`, `city`, `latitude/longitude`, `risk_profile` | `geog` column with PostGIS |
| `devices` | `device_id`, `device_type`, `fingerprint`, `first_seen`, `last_seen` | |
| `transactions` | `transaction_id`, `"timestamp"`, `sender_account_id`, `receiver_account_id`, `merchant_id`, `amount`, `currency`, `amount_usd`, `transaction_type`, `channel`, `country`, `latitude/longitude`, `device_id`, `ip_address` (inet), `status`, `external_counterparty` | A CHECK requires at least one internal party. Indexes on (sender, ts), (receiver, ts), (device, ts), merchant and ts. `amount_usd` uses static synthetic FX rates so that peer comparison works across currencies |

`external_counterparty` holds the other side of a transfer to or from another institution. Internal
transfers have both account ids set.

## Investigation entities

| Table | Purpose |
|---|---|
| `alerts` | **Seeded synthetic legacy-rule alerts** (`status` open / closed_*). They are loaded with the dataset to give investigations a starting queue. FIRA's risk engine does not create rows in this table; automated alert creation from risk scores is not implemented |
| `investigations` | Subject, status (`open → in_progress → pending_review → closed`, or `needs_input` / `failed`), conclusion, risk score, fired signals (`text[]`), summary and the full report (`jsonb`) |
| `evidence` | `ref` (E1…), `source_type` (database / metric / graph / document / ml / history), `source_id`, `evidence_type`, `title`, `content` (jsonb), `confidence`. Unique on (investigation, ref) |
| `human_decisions` | confirm / reject / escalate / request_more_evidence, rationale, `failure_categories`, report quality 1–5 |
| `agent_episodes` | request, plan, tool calls, observations, retrieved evidence refs, a **concise reasoning summary**, final output, evaluation metrics, human feedback, model, tokens, latency. It never stores model chain-of-thought |
| `audit_log` | Trail of user, role, action, tool, entity, result, request id, details. Append-only is enforced by triggers (revision 0002) in PostgreSQL mode; not cryptographically tamper-proof |

## Knowledge and governance

| Table | Purpose |
|---|---|
| `document_registry` | One row per ingested document: type, version, sha256, page count, parser, OCR pages |
| `document_chunks` | Chunk text with `page`, `section`, `ordinal`, plus a generated `tsv` (weighted section + body) with a GIN index for keyword search |
| `risk_config_versions` | proposed / validated / rejected / active / retired. A unique partial index guarantees at most one `active` |
| `evaluation_runs` | Metrics from every benchmark run |
| `users` | scrypt password hashes, role (analyst/admin) |
| `scenario_labels` | **Ground truth for the synthetic benchmark. Only the evaluation module reads it; no tool exposes it** |
| `dataset_manifest` | Generator seed, as-of date, counts |

## Synthetic dataset

`python -m app.synthetic.generator` (seeded, deterministic) produces:

| | default (10k) |
|---|---|
| customers | 10,000 (10% business; segments student/mass/affluent/sme/corporate; 18 cities in 10 countries, NG-weighted) |
| accounts | ~15,700 |
| merchants | 2,000 in 15 categories (gambling, crypto exchange, money transfer and jewelry are rated high-risk) |
| devices | ~14,000 (households legitimately share devices) |
| transactions | ~254,000 over 180 days (card, transfer, bill, cash, salary, deposit) |
| alerts / historical investigations | ~500 / 200 (legacy noise, independent of ground truth) |

Scenarios are injected into the final 30 days:

| Scenario | Suspicious | Injected pattern | Expected signals |
|---|---|---|---|
| account_takeover | yes | new browser device in a foreign city, 6–12 rapid transfers to new external beneficiaries | NEW_DEVICE, GEO_NEW_COUNTRY, TRANSACTION_BURST |
| mule_account | yes | 10–21 inbound transfers from distinct customers, 88–97% forwarded within hours | RAPID_PASS_THROUGH, FAN_IN |
| device_sharing_ring | yes | 4–6 customers transacting from one new device, transfers among them | DEVICE_SHARING |
| transaction_burst | yes | 18–34 small card-not-present payments within ~1 h, ~30% failed | TRANSACTION_BURST |
| circular_transfer | yes | 3–4 accounts in a cycle, two rounds, amounts decaying 1.5% per hop | CIRCULAR_FLOW |
| geographic_anomaly | yes | card-present at home, then in a city ≥2,500 km away within 2 h | IMPOSSIBLE_TRAVEL |
| dormant_reactivation | yes | no activity for ~130–150 days, then a large deposit and transfers out | DORMANT_REACTIVATION |
| high_risk_merchant | yes | 8–14 escalating payments to two high-risk merchants | HIGH_RISK_MERCHANT |
| high_frequency_legit | **no** | businesses with consistently high volume throughout history | — |
| legit_high_value | **no** | a single 15–30× payment to an education, real-estate or travel merchant | — |
| travel_legit | **no** | a realistic trip (flight-time consistent) with card use abroad | — |
| normal | **no** | sample of ordinary customers | — |

Output: `customers.csv.gz`, `customer_identifiers.csv.gz`, `accounts.csv.gz`, `merchants.csv.gz`,
`devices.csv.gz`, `transactions.csv.gz`, `alerts.csv.gz`, `historical_investigations.csv.gz`,
`scenario_labels.json`, `manifest.json`.

## Integrating a real (Gold-layer) source

Implement a function that returns the same column sets as `app/db/loader.py::LOAD_ORDER` and pass
it to `load()`. Required fields are those marked NOT NULL in `schema.sql`. Without `amount_usd`,
compute it from your FX reference table. Device, IP and geolocation fields are optional; the
detectors that need them report "not evaluated" or simply do not fire.
