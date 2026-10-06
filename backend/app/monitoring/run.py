"""Command-line monitoring run (for PostgreSQL mode, where alerts persist).

    python -m app.monitoring.run                       # customers with activity in the last day
    python -m app.monitoring.run --all                 # every customer (slow: ~0.1 s per customer)
    python -m app.monitoring.run --customers CUST-10686 CUST-17781 --lookback 30

In Frames mode (in-memory) alerts live only for the life of this process, so the run just prints a summary;
use the API/UI (`POST /api/monitoring/run`) to monitor a running Frames-mode server.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime

from app.config import get_settings
from app.security.principal import Principal


def main() -> None:
    ap = argparse.ArgumentParser(description="Run FIRA transaction monitoring")
    ap.add_argument("--customers", nargs="*", help="customer ids to screen (default: customers active in --active-days)")
    ap.add_argument("--all", action="store_true", help="screen every customer")
    ap.add_argument("--window-end", type=datetime.fromisoformat, default=None, help="ISO timestamp (default: dataset as-of)")
    ap.add_argument("--lookback", type=int, default=None)
    ap.add_argument("--active-days", type=int, default=None)
    a = ap.parse_args()

    from app.services.container import build_container

    c = build_container(get_settings())
    ids = c.store.list_customer_ids() if a.all else a.customers
    run = c.monitoring.run(window_end=a.window_end, lookback_days=a.lookback, customer_ids=ids,
                           active_days=a.active_days, actor=Principal("U-cli", "admin"), mode="cli",
                           max_customers=10**9)
    print(json.dumps(run.model_dump(mode="json"), indent=2, default=str))
    if get_settings().data_backend != "postgres":
        print("\nNote: Frames mode keeps alerts in memory only; they are gone when this process exits.")


if __name__ == "__main__":
    main()
