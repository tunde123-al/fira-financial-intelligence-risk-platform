# Backup, restore and disaster recovery

FIRA is a prototype running on synthetic data. This page describes what is built, what has been **tested**, and what is only a
target. A target is not an achievement: nothing below claims an RPO or RTO has been met unless it says "measured".

## 1. What is protected

The system of record is the PostgreSQL database: customers, accounts, transactions, alerts, cases, notes, evidence, the
append-only audit log, the ingestion ledger and quarantine, the configuration change log, risk-config versions and users.
Everything else is derivable or disposable:

| component | recovery |
|---|---|
| PostgreSQL | restore from a backup (this document) |
| in-memory graph (NetworkX) | rebuilt from PostgreSQL at start-up |
| Neo4j (optional) | not a system of record; rebuilt from PostgreSQL |
| Qdrant (optional) | not a system of record; re-ingest the documents |
| ML anomaly model | retrained from the data; not required |
| the **in-memory pandas store** (default for local runs) | **has no persistence at all.** Nothing to back up; a restart resets alerts and cases. Do not use it for anything you want to keep |

## 2. Backup tooling

`infrastructure/scripts/pg_backup.py` (Python, cross-platform, wraps `pg_dump` / `pg_restore`):

| command | does |
|---|---|
| `backup` | `pg_dump -Fc`, writes a manifest (UTC time, row counts of 13 key tables, Alembic revision, size, SHA-256, `pg_dump` version), optional AES-256-GCM encryption, optional pruning (`--keep N`) |
| `verify` | checksum against the manifest and `pg_restore --list` readability; works without a database |
| `restore` | restore into a **new** database you name; refuses to overwrite the source or an existing database |
| `restore-test` | create a scratch database, restore, compare every manifest row count, the Alembic revision and the four append-only triggers, print the measured restore time, drop the scratch database |

Passwords reach the tools through `PGPASSWORD`, never on a command line. `--docker-container NAME` runs the client tools inside a
container (used on the Windows development machine, where `pg_dump` is not installed). Encryption derives the key from a
passphrase held in an environment variable (scrypt, 32-byte key, AES-GCM with a random salt and nonce); a wrong passphrase or a
modified file fails with a clear error and writes nothing.

`infrastructure/scripts/verify_restored_app.py` starts the application against the restored database and checks readiness, the
schema revision, readable alerts/cases/batches (optionally against expected counts), that the audit trail is still append-only
(an `UPDATE` is refused by the trigger) and that the database accepts writes.

```bash
export DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/fira
export BACKUP_PASSPHRASE='...at least 12 characters...'      # keep it somewhere other than the backups
python infrastructure/scripts/pg_backup.py backup --out-dir backups --passphrase-env BACKUP_PASSPHRASE --keep 14
python infrastructure/scripts/pg_backup.py verify backups/fira_<stamp>.dump.enc --passphrase-env BACKUP_PASSPHRASE
python infrastructure/scripts/pg_backup.py restore-test backups/fira_<stamp>.dump.enc --passphrase-env BACKUP_PASSPHRASE
python infrastructure/scripts/pg_backup.py restore backups/fira_<stamp>.dump.enc --passphrase-env BACKUP_PASSPHRASE --target-db fira_restored
python infrastructure/scripts/verify_restored_app.py --url postgresql://USER:PASSWORD@HOST:5432/fira_restored
```

## 3. Policy (targets, not achievements)

| item | value | status |
|---|---|---|
| frequency | daily full backup; before every migration; before any bulk load | **target**: nothing schedules the script. Run it from cron, a scheduled task or a CI job |
| retention | newest 14 daily backups plus the newest backup of each of the last 6 months | **target**; `--keep N` implements only the "newest N" part |
| storage | the archive must be copied off the database host to separate storage | **not automated** |
| encryption | `--passphrase-env` encrypts the archive; the passphrase must live apart from the archive | implemented, tested |
| restore test | after every change to the backup tooling, and at least monthly | `restore-test` exists and passed (section 5); the monthly schedule is a target |
| RPO (data loss tolerated) | **24 hours** with daily backups | **target** only. Not achieved or measured; there is no point-in-time recovery or WAL archiving |
| RTO (time to restore service) | **1 hour** for a database of this size | **target** only. What was measured is the restore step alone (section 5) |

**Managed and free hosting.** A free PostgreSQL tier (including Render's free database) has *no* backups you can rely on and may
expire. Do not read this document as a promise that a free deployment is backed up: it is not unless you run the script and store
the archive somewhere else. Paid managed tiers add provider snapshots and point-in-time recovery whose terms you must read yourself;
FIRA does not verify them.

## 4. Failure scenarios and recovery

| scenario | what happens | recovery |
|---|---|---|
| database unreachable at start-up | the API **fails to start within the connect timeout (5 s default, `DB_CONNECT_TIMEOUT_S`)** with a clear error; the password is not printed. (It took 136 s before a timeout was set: found by the failure test.) | fix connectivity; restart |
| database goes away while running | `/health` stays up (liveness); `/health/ready` returns 503 with `database: down`; requests that need the database return a safe 500 with a request id | restore connectivity; connections killed by a restart are replaced automatically (`pool_pre_ping`, tested with `pg_terminate_backend`) |
| Qdrant unreachable | the application starts; readiness is `degraded` (503) with `vector_store: down`; monitoring, alerts, cases, graph and the data-quality screens keep working; document search is unavailable | restore Qdrant, re-ingest documents |
| invalid monitoring or risk configuration | the process refuses to start with a validation error (nothing silently falls back to defaults) | fix the file |
| bad migration | each migration runs in one transaction (`transaction_per_migration`): a failing 0004 leaves **no** tables or columns behind (tested) | fix the SQL; run again; 0004 is idempotent and reversible (`alembic downgrade 0003`) |
| database behind the code (migration not applied) | `/health/ready` returns 503 with `schema_current: false` and the revision found | run `alembic upgrade head` |
| process restart | stored alerts, cases, batches and config history are intact; the baseline ingestion batch and the configuration baseline are **not** duplicated (tested) | none |
| corrupted or tampered backup | `verify` / `restore` stop on a checksum mismatch; a modified encrypted file or a wrong passphrase is refused | use an earlier backup |
| accidental data deletion in the application | the audit log, alert events, case events and the ingestion/config ledgers are append-only at the database level; other tables are not | restore into a scratch database with `restore`, copy what is needed back |
| loss of the whole database host | everything since the last backup is lost | restore the latest archive into a new database (section 5); RPO is the age of that archive |

## 5. What was actually tested

On the development machine (Windows 10, 4 logical CPUs, 7.9 GB RAM, PostgreSQL 15.19 in Docker, a 600-customer / 15,159-transaction /
94-alert synthetic database with 199 audit rows, 1 case, 2 ingestion batches), on 2026-10-03:

| test | result |
|---|---|
| `backup` | 820,114 bytes, `pg_dump` 0.46 s; manifest with 13 table row counts and revision 0004 |
| `verify` (plain and encrypted) | checksum matches; `pg_restore --list` reads 208 entries |
| `restore-test` | **restore 20.97 s** (measured, including `docker exec` start-up and `--exit-on-error`); all 13 row counts equal the manifest; revision matches; all 4 append-only triggers present; scratch database dropped |
| `restore` into a named database, then `verify_restored_app.py` | ready, schema 0004 current; 94 alerts, 1 case, 2 batches as expected; `UPDATE audit_log` refused by the trigger; new writes accepted |
| encrypted backup, wrong passphrase | refused, exit code 2, nothing written |
| modified archive | checksum mismatch, exit code 1 |
| restore over the source database | refused |
| connections terminated with `pg_terminate_backend` | next request succeeds |
| unreachable database | start-up fails in ~13 s test time (connect timeout 5 s) |

What this does **not** show: restore time or backup size at production scale (the database is tiny), restore on a different host,
point-in-time recovery, behaviour under concurrent load while a backup runs, or that the archive reached safe storage. A 1-hour
RTO and 24-hour RPO are therefore *targets that this evidence neither confirms nor refutes*.

## 6. Procedure after a loss

1. Stop writers (stop the API) so nothing is written to a database you are about to replace.
2. Provision a new PostgreSQL 15+ server and an empty database.
3. `pg_backup.py verify` the newest archive; fall back to the previous one on failure.
4. `pg_backup.py restore ... --target-db fira` against the new server.
5. Check the revision (`/health/ready` after step 6, or `SELECT version_num FROM alembic_version`) and run `alembic upgrade head` if the code is newer than the backup.
6. Point `DATABASE_URL` at the restored database and start the API; run `verify_restored_app.py`.
7. Record the incident, the age of the archive used (that is the data lost) and the time taken (that is the measured RTO for this event).
