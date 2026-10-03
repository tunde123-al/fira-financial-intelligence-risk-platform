"""Load the synthetic dataset into PostgreSQL.

    python -m app.db.loader --dataset ../data/seeds [--truncate]

`prepare_frames` (pure pandas) converts the generator output into exactly the
column order of each table; `load` streams each frame with PostgreSQL COPY.
Tables are loaded in foreign-key order inside one transaction.
"""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

import pandas as pd

LOAD_ORDER: list[tuple[str, str, list[str]]] = [
    ("customers", "customers", ["customer_id", "customer_type", "segment", "created_at", "country", "home_city",
                                "home_latitude", "home_longitude", "risk_profile", "status", "kyc_level"]),
    ("customer_identifiers", "customer_identifiers", ["customer_id", "identifier_type", "value_hash", "masked_value"]),
    ("accounts", "accounts", ["account_id", "customer_id", "account_type", "currency", "opened_at", "status", "balance"]),
    ("merchants", "merchants", ["merchant_id", "name", "category", "country", "city", "latitude", "longitude",
                                "risk_profile"]),
    ("devices", "devices", ["device_id", "device_type", "fingerprint", "first_seen", "last_seen"]),
    ("transactions", "transactions", ["transaction_id", "timestamp", "sender_account_id", "receiver_account_id",
                                      "merchant_id", "amount", "currency", "amount_usd", "transaction_type", "channel",
                                      "country", "latitude", "longitude", "device_id", "ip_address", "status",
                                      "external_counterparty"]),
    ("alerts", "alerts", ["alert_id", "entity_id", "entity_type", "alert_type", "severity", "risk_score",
                          "created_at", "status"]),
    ("historical_investigations", "investigations", ["investigation_id", "subject_id", "subject_type", "assigned_to",
                                                     "status", "created_at", "closed_at", "conclusion", "signals",
                                                     "summary"]),
]


def _pg_array(values: list[str]) -> str:
    def q(v: str) -> str:
        return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'
    return "{" + ",".join(q(v) for v in values) + "}"


def prepare_frames(dataset_dir: Path) -> dict[str, tuple[list[str], pd.DataFrame]]:
    dataset_dir = Path(dataset_dir)
    out: dict[str, tuple[list[str], pd.DataFrame]] = {}
    for fname, table, cols in LOAD_ORDER:
        df = pd.read_csv(dataset_dir / f"{fname}.csv.gz")
        if table == "investigations":
            df["signals"] = df["signals"].map(lambda s: _pg_array(json.loads(s)) if isinstance(s, str) and s else "{}")
        out[table] = (cols, df[cols])
    labels = json.loads((dataset_dir / "scenario_labels.json").read_text(encoding="utf-8"))
    lab = pd.DataFrame(labels)
    lab["expected_signals"] = lab.expected_signals.map(_pg_array)
    lab["related_entities"] = lab.related_entities.map(_pg_array)
    lab = lab.drop_duplicates(subset=["entity_type", "entity_id", "scenario"])
    lcols = ["entity_type", "entity_id", "scenario", "is_suspicious", "window_start", "window_end",
             "expected_signals", "related_entities", "notes"]
    out["scenario_labels"] = (lcols, lab[lcols])
    return out


def write_csvs(frames: dict[str, tuple[list[str], pd.DataFrame]], out_dir: Path) -> None:
    """Write load-ready CSVs (used to verify the load path with `psql \\copy`)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for table, (_, df) in frames.items():
        df.to_csv(out_dir / f"{table}.csv", index=False)


def load(database_url: str, dataset_dir: Path, truncate: bool = False) -> dict[str, int]:
    from sqlalchemy import create_engine, text

    frames = prepare_frames(dataset_dir)
    manifest = json.loads((Path(dataset_dir) / "manifest.json").read_text(encoding="utf-8"))
    engine = create_engine(database_url)
    counts: dict[str, int] = {}
    with engine.begin() as conn:
        if truncate:
            conn.execute(text("TRUNCATE scenario_labels, evidence, human_decisions, agent_episodes, investigations, "
                              "alerts, transactions, devices, merchants, accounts, customer_identifiers, customers "
                              "RESTART IDENTITY CASCADE"))
        raw = conn.connection.dbapi_connection  # psycopg 3 connection
        with raw.cursor() as cur:  # type: ignore[union-attr]
            for table, (cols, df) in frames.items():
                buf = io.StringIO()
                df.to_csv(buf, index=False, header=False)
                col_sql = ", ".join(f'"{c}"' for c in cols)
                with cur.copy(f"COPY {table} ({col_sql}) FROM STDIN WITH (FORMAT csv)") as cp:
                    cp.write(buf.getvalue())
                counts[table] = int(len(df))
        conn.execute(text("INSERT INTO dataset_manifest (id, manifest) VALUES (1, CAST(:m AS jsonb)) "
                          "ON CONFLICT (id) DO UPDATE SET manifest = EXCLUDED.manifest, loaded_at = now()"),
                     {"m": json.dumps(manifest)})
        conn.execute(text("ANALYZE"))
    return counts


def main() -> None:
    from app.config import get_settings

    p = argparse.ArgumentParser()
    p.add_argument("--dataset", type=Path, default=None)
    p.add_argument("--truncate", action="store_true")
    p.add_argument("--csv-out", type=Path, default=None, help="only write load-ready CSVs to this directory")
    a = p.parse_args()
    s = get_settings()
    ds = a.dataset or s.dataset_dir
    if a.csv_out:
        write_csvs(prepare_frames(ds), a.csv_out)
        print(f"wrote CSVs to {a.csv_out}")
        return
    if not s.database_url:
        raise SystemExit("DATABASE_URL is not set")
    print(json.dumps(load(s.database_url, ds, a.truncate), indent=2))


if __name__ == "__main__":
    main()
