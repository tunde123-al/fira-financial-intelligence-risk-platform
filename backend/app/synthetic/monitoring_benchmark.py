"""Independent labelled dataset for evaluating the transaction-monitoring pipeline.

It is the standard FIRA synthetic bank (same base behaviour and the eight original suspicious scenarios and
three look-alike traps) generated with its **own seed**, plus scenarios the original benchmark does not
contain:

* ``structuring_detectable``  near-threshold transactions clustered inside 24 hours
* ``structuring_evasive``     near-threshold transactions spaced just over 24 hours apart. These ARE suspicious
                              by construction but the STRUCTURING detector is not designed to catch them, so
                              recall below 1.0 is expected and honest
* ``fan_out_detectable``      many distinct beneficiaries in 36 hours
* ``fan_out_evasive``         5 to 7 beneficiaries (below the detector threshold)
* hard negatives that look suspicious but are benign: recurring large business payments, payroll runs, and
  first-ever invoice batches / payroll runs by new businesses (ambiguous on purpose: expect false positives)

Added for the production-oriented upgrade (money-mule and triage evaluation):

* ``mule_to_collector``       fan-in from 6 to 10 senders, 85-95% forwarded within hours to a shared collector
* ``mule_collector``          the individual that receives pooled funds from several mules (network scenario)
* ``mule_slow_evasive``       fan-in, but the funds leave 30-60 h later (outside the 24 h rapid-movement window).
                              Suspicious by construction; the rapid-movement indicator is NOT expected to fire
* ``fan_in_collection``       12-18 senders in two days, bulk cash-out 36-60 h later
* ``rapid_single_pass_through`` one large inbound transfer leaves again within three hours
* benign look-alikes: marketplace sellers (fan-in, funds retained), family collections (fan-in plus a prompt
  forward: ambiguous on purpose) and rent splits (rapid pass-through of three small inbound transfers)

Scenario code never reads the labels it writes; the evaluator reads labels only to score outputs.

The default dataset (`app.synthetic.generator`) is untouched by this module.

Usage:
    python -m app.synthetic.monitoring_benchmark --out ../data/benchmark --customers 4000 --seed 2024
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from app.synthetic.generator import GeneratorConfig, SyntheticBank

NEW_SUSPICIOUS = {"structuring_detectable", "structuring_evasive", "fan_out_detectable", "fan_out_evasive",
                  "mule_to_collector", "mule_collector", "mule_slow_evasive", "fan_in_collection",
                  "rapid_single_pass_through"}
NEW_BENIGN = {"legit_recurring_large_payments", "legit_new_business_invoices", "legit_payroll", "legit_first_payroll",
              "legit_marketplace_seller", "legit_family_collection", "legit_rent_split"}
NEW_EXPECTED = {
    "structuring_detectable": ["STRUCTURING"], "fan_out_detectable": ["FAN_OUT"],
    "structuring_evasive": [], "fan_out_evasive": [],
    "mule_to_collector": ["RAPID_PASS_THROUGH", "FAN_IN"], "mule_collector": [], "mule_slow_evasive": [],
    "fan_in_collection": ["FAN_IN"], "rapid_single_pass_through": ["RAPID_PASS_THROUGH"],
}
# scenarios that are money-mule typologies (used by the mule evaluation); everything else is a negative for it
MULE_TYPOLOGIES = {"mule_account", "mule_to_collector", "mule_collector", "mule_slow_evasive", "fan_in_collection"}
# counts per 10,000 customers (scaled by n_customers)
NEW_COUNTS = {
    "structuring_detectable": 25, "structuring_evasive": 25, "fan_out_detectable": 20, "fan_out_evasive": 20,
    "legit_recurring_large_payments": 25, "legit_new_business_invoices": 15, "legit_payroll": 20,
    "legit_first_payroll": 10,
    "mule_chain_group": 10, "mule_slow_evasive": 20, "fan_in_collection": 20, "rapid_single_pass_through": 20,
    "legit_marketplace_seller": 20, "legit_family_collection": 15, "legit_rent_split": 20,
}


@dataclass
class BenchmarkConfig(GeneratorConfig):
    new_counts: dict[str, int] = field(default_factory=lambda: dict(NEW_COUNTS))
    # False reproduces the benchmark exactly as it was before the money-mule scenarios were added (the v2 results files)
    include_mule_family: bool = True

    def scaled_new(self, key: str) -> int:
        return max(2, int(round(self.new_counts[key] * self.n_customers / 10_000)))


class MonitoringBenchmarkBank(SyntheticBank):
    cfg: BenchmarkConfig

    def __init__(self, cfg: BenchmarkConfig):
        super().__init__(cfg)
        self.suspicious_scenarios |= NEW_SUSPICIOUS
        self.expected_signals.update(NEW_EXPECTED)

    # ------------------------------------------------------------------ helpers
    def _event_time(self) -> pd.Timestamp:
        return self._ts(self.window_start + pd.Timedelta(days=3), self.as_of - pd.Timedelta(days=3))

    def _txn(self, cid: str, ts: pd.Timestamp, usd: float, ttype: str = "transfer", ext: str | None = None,
             receiver: str | None = None) -> None:
        r, acc, cur, devs = self._cust_ctx(cid)
        channel = "atm" if ttype == "cash_withdrawal" else "web"
        self._add_txn(timestamp=ts, sender_account_id=acc, receiver_account_id=receiver, amount=self._usd_to(usd, cur),
                      currency=cur, transaction_type=ttype, channel=channel, country=r["country"],
                      latitude=r["home_latitude"], longitude=r["home_longitude"], device_id=devs[0],
                      ip_address=self.device_ip[devs[0]], external_counterparty=None if receiver else ext)

    # ------------------------------------------------------------------ scenarios
    def structuring(self, cids: list[str], detectable: bool) -> None:
        rng = self.rng
        for cid in cids:
            ttype = str(rng.choice(["transfer", "cash_withdrawal"]))
            ext = f"EXT-STRUCT-{int(rng.integers(1000, 9999))}"
            n = int(rng.integers(3, 7)) if detectable else int(rng.integers(4, 7))
            t = self._event_time()
            for _ in range(n):
                if t >= self.as_of:
                    break
                self._txn(cid, t, float(rng.uniform(8200, 9950)), ttype, ext if ttype == "transfer" else None)
                t = t + pd.Timedelta(hours=float(rng.uniform(0.5, 3.0) if detectable else rng.uniform(25.0, 30.0)))
            name = "structuring_detectable" if detectable else "structuring_evasive"
            self._label(cid, name, notes=f"{n} near-threshold {ttype}s, "
                                         + ("clustered in a day" if detectable else "spaced >24h apart"))

    def fan_out(self, cids: list[str], detectable: bool) -> None:
        rng = self.rng
        for cid in cids:
            n = int(rng.integers(9, 17)) if detectable else int(rng.integers(5, 8))
            t0 = self._event_time()
            for i in range(n):
                ts = t0 + pd.Timedelta(hours=float(i * rng.uniform(0.5, 3.0)))
                if ts >= self.as_of:
                    break
                self._txn(cid, ts, float(rng.uniform(40, 400)), "transfer", f"EXT-FAN-{int(rng.integers(10**5, 10**6))}")
            self._label(cid, "fan_out_detectable" if detectable else "fan_out_evasive",
                        notes=f"{n} distinct beneficiaries within ~36h")

    def legit_recurring_large(self, cids: list[str]) -> None:
        """Business pays the same supplier-sized invoices every month, in the baseline and in the window."""
        rng = self.rng
        for k, cid in enumerate(cids):
            batch_day = k % 2 == 0  # half pay the three invoices on one day, half spread them over the month
            ext = f"EXT-SUPPLIER-{int(rng.integers(1000, 9999))}"
            t_win = self._event_time()
            for m in range(0, 5):
                base = t_win - pd.Timedelta(days=30 * m)
                for i in range(3):
                    off = pd.Timedelta(hours=float(i * rng.uniform(0.5, 2.0))) if batch_day else pd.Timedelta(days=i * 9)
                    ts = base + off
                    if ts < self.as_of and ts > self.history_start:
                        self._txn(cid, ts, float(rng.uniform(8300, 9800)), "transfer", ext)
            self._label(cid, "legit_recurring_large_payments",
                        notes="recurring supplier payments just under USD 10k, present in the baseline")

    def legit_new_business_invoices(self, cids: list[str]) -> None:
        """First time a business pays a batch of large invoices: ambiguous by design."""
        rng = self.rng
        for cid in cids:
            t0 = self._event_time()
            ext = f"EXT-INVOICE-{int(rng.integers(1000, 9999))}"
            for i in range(3):
                self._txn(cid, t0 + pd.Timedelta(hours=float(i * rng.uniform(0.5, 4.0))), float(rng.uniform(8500, 9900)),
                          "transfer", ext)
            self._label(cid, "legit_new_business_invoices", notes="first invoice batch of a business, no history of it")

    def legit_payroll(self, cids: list[str], recurring: bool, pool: list[str]) -> None:
        rng = self.rng
        for cid in cids:
            n = int(rng.integers(10, 15))
            staff = [self.primary_account[c] for c in rng.choice(pool, size=n, replace=False) if c != cid]
            t_win = self._event_time()
            months = range(0, 5) if recurring else range(0, 1)
            for m in months:
                base = t_win - pd.Timedelta(days=30 * m)
                for i, acc in enumerate(staff):
                    ts = base + pd.Timedelta(minutes=float(i * rng.uniform(1, 6)))
                    if ts < self.as_of and ts > self.history_start:
                        self._txn(cid, ts, float(rng.uniform(900, 2200)), "transfer", receiver=acc)
            self._label(cid, "legit_payroll" if recurring else "legit_first_payroll",
                        notes=f"payroll run to {len(staff)} employees" + ("" if recurring else " (first ever)"))

    # ------------------------------------------------------------------ money-mule family (v3)
    def _transfer(self, s: str, r_acc: str, ts: pd.Timestamp, usd: float, ext: str | None = None) -> None:
        """Transfer from customer `s`'s primary account to an account of the bank (or an external counterparty)."""
        s_acc = self.primary_account[s]
        s_cur = self.acc_index[s_acc]["currency"]
        sr, sd = self.cust_index[s], self.devices_of[s][0]
        self._add_txn(timestamp=ts, sender_account_id=s_acc, receiver_account_id=r_acc, amount=self._usd_to(usd, s_cur),
                      currency=s_cur, transaction_type="transfer", channel="mobile", country=sr["country"],
                      latitude=sr["home_latitude"], longitude=sr["home_longitude"], device_id=sd,
                      ip_address=self.device_ip[sd], external_counterparty=ext if r_acc is None else None)

    def _fan_in_to(self, cid: str, pool: list[str], n: int, t0: pd.Timestamp, spread_h: float, lo: float,
                   hi: float) -> tuple[float, pd.Timestamp]:
        """`n` distinct senders from `pool` pay `cid`. Returns (total USD, time of the last inbound)."""
        rng = self.rng
        senders = [c for c in rng.choice(pool, size=min(n + 2, len(pool)), replace=False) if c != cid][:n]
        total, last = 0.0, t0
        for j, snd in enumerate(senders):
            usd = float(rng.uniform(lo, hi))
            ts = t0 + pd.Timedelta(hours=float(j * spread_h / max(len(senders), 1) * rng.uniform(0.6, 1.4)))
            if ts >= self.as_of - pd.Timedelta(hours=2):
                break
            self._transfer(snd, self.primary_account[cid], ts, usd)
            total, last = total + usd, max(last, ts)
        return total, last

    def mule_chains(self, groups: int, pool: list[str]) -> None:
        rng = self.rng
        for _ in range(groups):
            members = self._pick_unused(pool, 4)
            if len(members) < 4:
                return
            collector, mules = members[0], members[1:]
            coll_acc = self.primary_account[collector]
            for m in mules:
                t0 = self._ts(self.window_start + pd.Timedelta(days=3), self.as_of - pd.Timedelta(days=8))
                total, last = self._fan_in_to(m, pool, int(rng.integers(6, 11)), t0, 14.0, 180, 700)
                if total <= 0:
                    continue
                self._transfer(m, coll_acc, last + pd.Timedelta(hours=float(rng.uniform(0.5, 5))),
                               total * float(rng.uniform(0.85, 0.95)))
                self._label(m, "mule_to_collector", related=[collector],
                            notes=f"fan-in ~USD {total:,.0f}, ~90% forwarded to a collector within hours")
            self._label(collector, "mule_collector", related=list(mules),
                        notes="receives pooled funds from several mule accounts")

    def mule_slow(self, cids: list[str], pool: list[str]) -> None:
        rng = self.rng
        for cid in cids:
            t0 = self._ts(self.window_start + pd.Timedelta(days=3), self.as_of - pd.Timedelta(days=10))
            total, last = self._fan_in_to(cid, pool, int(rng.integers(6, 9)), t0, 20.0, 200, 800)
            out_ts = last + pd.Timedelta(hours=float(rng.uniform(30, 60)))
            if total > 0 and out_ts < self.as_of:
                self._txn(cid, out_ts, total * float(rng.uniform(0.85, 0.95)), "transfer",
                          f"EXT-MULE-SLOW-{int(rng.integers(100, 999))}")
            self._label(cid, "mule_slow_evasive", notes=f"fan-in ~USD {total:,.0f}, forwarded 30-60 h later")

    def fan_in_collection(self, cids: list[str], pool: list[str]) -> None:
        rng = self.rng
        for cid in cids:
            t0 = self._ts(self.window_start + pd.Timedelta(days=3), self.as_of - pd.Timedelta(days=10))
            total, last = self._fan_in_to(cid, pool, int(rng.integers(12, 19)), t0, 40.0, 120, 600)
            out_ts = last + pd.Timedelta(hours=float(rng.uniform(36, 60)))
            if total > 0 and out_ts < self.as_of:
                self._txn(cid, out_ts, total * float(rng.uniform(0.75, 0.9)), "cash_withdrawal")
            self._label(cid, "fan_in_collection", notes=f"fan-in ~USD {total:,.0f}, bulk cash-out 36-60 h later")

    def rapid_single(self, cids: list[str], pool: list[str]) -> None:
        rng = self.rng
        for cid in cids:
            t0 = self._event_time()
            src = str(rng.choice([c for c in pool if c != cid]))
            usd = float(rng.uniform(8000, 20000))
            self._transfer(src, self.primary_account[cid], t0, usd)
            for _ in range(int(rng.integers(2, 4))):
                self._txn(cid, t0 + pd.Timedelta(hours=float(rng.uniform(0.2, 3.0))), usd * float(rng.uniform(0.28, 0.33)),
                          "transfer", f"EXT-PASS-{int(rng.integers(100, 999))}")
            self._label(cid, "rapid_single_pass_through", notes=f"one inbound USD {usd:,.0f} leaves within 3 h")

    def legit_marketplace(self, cids: list[str], pool: list[str]) -> None:
        rng = self.rng
        for cid in cids:
            t0 = self._ts(self.window_start + pd.Timedelta(days=3), self.as_of - pd.Timedelta(days=12))
            total, last = self._fan_in_to(cid, pool, int(rng.integers(10, 17)), t0, 200.0, 40, 300)
            out_ts = last + pd.Timedelta(hours=float(rng.uniform(72, 150)))
            if total > 0 and out_ts < self.as_of:
                self._txn(cid, out_ts, total * float(rng.uniform(0.2, 0.4)), "transfer",
                          f"EXT-SUPPLIER-{int(rng.integers(1000, 9999))}")
            self._label(cid, "legit_marketplace_seller", notes="many small customer payments, funds mostly retained")

    def legit_family(self, cids: list[str], pool: list[str]) -> None:
        rng = self.rng
        for cid in cids:
            t0 = self._ts(self.window_start + pd.Timedelta(days=3), self.as_of - pd.Timedelta(days=8))
            total, last = self._fan_in_to(cid, pool, int(rng.integers(6, 10)), t0, 40.0, 100, 250)
            out_ts = last + pd.Timedelta(hours=float(rng.uniform(4, 20)))
            if total > 0 and out_ts < self.as_of:
                self._txn(cid, out_ts, total * float(rng.uniform(0.9, 0.99)), "transfer", f"EXT-GIFT-{int(rng.integers(100, 999))}")
            self._label(cid, "legit_family_collection",
                        notes="relatives pool money for a shared gift, forwarded within a day (ambiguous on purpose)")

    def legit_rent_split(self, cids: list[str], pool: list[str]) -> None:
        rng = self.rng
        for cid in cids:
            t0 = self._event_time()
            total, last = self._fan_in_to(cid, pool, 3, t0, 2.0, 400, 700)
            if total > 0:
                self._txn(cid, last + pd.Timedelta(hours=float(rng.uniform(0.5, 2))), total, "transfer",
                          f"EXT-LANDLORD-{int(rng.integers(100, 999))}")
            self._label(cid, "legit_rent_split", notes="three housemates pay in, the full rent is forwarded to a landlord")

    # ------------------------------------------------------------------ hook
    def inject_extra(self, individuals: list[str], businesses: list[str]) -> None:
        cfg = self.cfg
        self.structuring(self._pick_unused(individuals, cfg.scaled_new("structuring_detectable")), True)
        self.structuring(self._pick_unused(individuals, cfg.scaled_new("structuring_evasive")), False)
        self.fan_out(self._pick_unused(individuals, cfg.scaled_new("fan_out_detectable")), True)
        self.fan_out(self._pick_unused(individuals, cfg.scaled_new("fan_out_evasive")), False)
        self.legit_recurring_large(self._pick_unused(businesses, cfg.scaled_new("legit_recurring_large_payments")))
        self.legit_new_business_invoices(self._pick_unused(businesses, cfg.scaled_new("legit_new_business_invoices")))
        self.legit_payroll(self._pick_unused(businesses, cfg.scaled_new("legit_payroll")), True, individuals)
        self.legit_payroll(self._pick_unused(businesses, cfg.scaled_new("legit_first_payroll")), False, individuals)
        if not cfg.include_mule_family:
            return
        self.mule_chains(cfg.scaled_new("mule_chain_group"), individuals)
        self.mule_slow(self._pick_unused(individuals, cfg.scaled_new("mule_slow_evasive")), individuals)
        self.fan_in_collection(self._pick_unused(individuals, cfg.scaled_new("fan_in_collection")), individuals)
        self.rapid_single(self._pick_unused(individuals, cfg.scaled_new("rapid_single_pass_through")), individuals)
        self.legit_marketplace(self._pick_unused(businesses, cfg.scaled_new("legit_marketplace_seller")), individuals)
        self.legit_family(self._pick_unused(individuals, cfg.scaled_new("legit_family_collection")), individuals)
        self.legit_rent_split(self._pick_unused(individuals, cfg.scaled_new("legit_rent_split")), individuals)


def generate_monitoring_benchmark(out: Path, n_customers: int = 4000, seed: int = 2024,
                                  history_days: int = 180, include_mule_family: bool = True) -> dict[str, Any]:
    cfg = BenchmarkConfig(n_customers=n_customers, seed=seed, history_days=history_days,
                          include_mule_family=include_mule_family)
    bank = MonitoringBenchmarkBank(cfg)
    bank.generate()
    manifest = bank.write(out)
    manifest["generator"] = "fira-monitoring-benchmark"
    manifest["new_scenarios"] = sorted(NEW_SUSPICIOUS | NEW_BENIGN)
    manifest["include_mule_family"] = include_mule_family
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return manifest


def main() -> None:
    p = argparse.ArgumentParser(description="Generate the FIRA monitoring benchmark dataset")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--customers", type=int, default=4000)
    p.add_argument("--seed", type=int, default=2024)
    p.add_argument("--without-mule-family", action="store_true",
                   help="omit the money-mule scenarios and look-alikes (reproduces the v2 benchmark)")
    a = p.parse_args()
    print(json.dumps(generate_monitoring_benchmark(a.out, a.customers, a.seed, include_mule_family=not a.without_mule_family),
                     indent=2, default=str))


if __name__ == "__main__":
    main()

