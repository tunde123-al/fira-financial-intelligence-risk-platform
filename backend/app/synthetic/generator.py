"""Synthetic financial dataset generator with ground-truth scenario labels.

Produces a self-consistent bank dataset (customers, identifiers, accounts,
merchants, devices, transactions, alerts, historical investigations) and injects
known typologies into the final `window_days` before `as_of`. Every injected
scenario is recorded in `scenario_labels.json` together with the risk signals a
correct detector is expected to raise. The evaluation framework uses these labels;
the investigation agent never sees them.

Usage:
    python -m app.synthetic.generator --out ../data/seeds --customers 10000
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.synthetic import reference as ref

TXN_COLUMNS = [
    "timestamp", "sender_account_id", "receiver_account_id", "merchant_id", "amount",
    "currency", "amount_usd", "transaction_type", "channel", "country", "latitude",
    "longitude", "device_id", "ip_address", "status", "external_counterparty",
]

SCENARIO_EXPECTED_SIGNALS: dict[str, list[str]] = {
    "account_takeover": ["NEW_DEVICE", "GEO_NEW_COUNTRY", "TRANSACTION_BURST"],
    "mule_account": ["RAPID_PASS_THROUGH", "FAN_IN"],
    "device_sharing_ring": ["DEVICE_SHARING"],
    "transaction_burst": ["TRANSACTION_BURST"],
    "circular_transfer": ["CIRCULAR_FLOW"],
    "geographic_anomaly": ["IMPOSSIBLE_TRAVEL"],
    "dormant_reactivation": ["DORMANT_REACTIVATION"],
    "high_risk_merchant": ["HIGH_RISK_MERCHANT"],
    # false-positive traps: legitimate behaviour that superficially resembles risk
    "high_frequency_legit": [],
    "legit_high_value": [],
    "travel_legit": [],
}
SUSPICIOUS_SCENARIOS = {
    "account_takeover", "mule_account", "device_sharing_ring", "transaction_burst",
    "circular_transfer", "geographic_anomaly", "dormant_reactivation", "high_risk_merchant",
}


@dataclass
class GeneratorConfig:
    n_customers: int = 10_000
    history_days: int = 180
    window_days: int = 30
    as_of: datetime = datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc)
    seed: int = 42
    # scenario counts are scaled with n_customers relative to 10k
    scenario_counts: dict[str, int] = field(default_factory=lambda: {
        "account_takeover": 30, "mule_account": 30, "device_sharing_ring": 8,  # rings
        "transaction_burst": 30, "circular_transfer": 10,  # cycles
        "geographic_anomaly": 30, "dormant_reactivation": 25, "high_risk_merchant": 25,
        "high_frequency_legit": 30, "legit_high_value": 30, "travel_legit": 25,
    })
    n_normal_labels: int = 300
    hash_salt: str = "fira-synthetic-salt"

    def scaled(self, key: str) -> int:
        factor = self.n_customers / 10_000
        return max(2, int(round(self.scenario_counts[key] * factor)))


def _hash(value: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()


class SyntheticBank:
    def __init__(self, cfg: GeneratorConfig):
        self.cfg = cfg
        self.rng = np.random.default_rng(cfg.seed)
        self.as_of = pd.Timestamp(cfg.as_of)
        self.history_start = self.as_of - pd.Timedelta(days=cfg.history_days)
        self.window_start = self.as_of - pd.Timedelta(days=cfg.window_days)
        self.txn_rows: list[dict[str, Any]] = []
        self.labels: list[dict[str, Any]] = []
        self.used_customers: set[str] = set()
        self.extra_devices: list[dict[str, Any]] = []
        self.device_users: dict[str, set[str]] = {}

    # ------------------------------------------------------------------ helpers
    def _choice_weighted(self, mapping: dict[str, float], size: int) -> np.ndarray:
        keys = list(mapping)
        w = np.array([mapping[k] for k in keys], dtype=float)
        return self.rng.choice(keys, size=size, p=w / w.sum())

    def _ip_for_country(self, country: str) -> str:
        first = {"NG": [41, 102, 105, 197], "GH": [154, 41], "KE": [41, 105], "ZA": [41, 196],
                 "GB": [81, 86, 92], "US": [24, 67, 98], "CA": [24, 99], "AE": [94, 5],
                 "CN": [36, 58], "TR": [78, 88]}.get(country, [100])
        o = self.rng.integers(1, 255, size=3)
        return f"{self.rng.choice(first)}.{o[0]}.{o[1]}.{o[2]}"

    def _city(self, name: str) -> tuple[str, str, float, float]:
        for c, country, lat, lon, _ in ref.CITIES:
            if c == name:
                return c, country, lat, lon
        raise KeyError(name)

    def _jitter(self, lat: float, lon: float, scale: float = 0.04) -> tuple[float, float]:
        d = self.rng.normal(0, scale, size=2)
        return round(lat + d[0], 5), round(lon + d[1], 5)

    def _ts(self, start: pd.Timestamp, end: pd.Timestamp) -> pd.Timestamp:
        span = (end - start).total_seconds()
        return start + pd.Timedelta(seconds=float(self.rng.uniform(0, max(span, 1))))

    def _usd_to(self, usd: float, currency: str) -> float:
        return round(usd * ref.FX_PER_USD[currency], 2)

    def _add_txn(self, **kw: Any) -> None:
        row: dict[str, Any] = {c: None for c in TXN_COLUMNS}
        row.update(kw)
        row.setdefault("status", "completed")
        if row["status"] is None:
            row["status"] = "completed"
        cur = row["currency"]
        row["amount_usd"] = round(float(row["amount"]) / ref.FX_PER_USD[cur], 2)
        self.txn_rows.append(row)

    def _pick_unused(self, pool: list[str], n: int) -> list[str]:
        candidates = [c for c in pool if c not in self.used_customers]
        picked = list(self.rng.choice(candidates, size=min(n, len(candidates)), replace=False))
        self.used_customers.update(picked)
        return picked

    def _label(self, entity_id: str, scenario: str, related: list[str] | None = None,
               entity_type: str = "customer", notes: str = "") -> None:
        self.labels.append({
            "entity_type": entity_type, "entity_id": entity_id, "scenario": scenario,
            "is_suspicious": scenario in SUSPICIOUS_SCENARIOS,
            "window_start": self.window_start.isoformat(), "window_end": self.as_of.isoformat(),
            "expected_signals": SCENARIO_EXPECTED_SIGNALS.get(scenario, []),
            "related_entities": related or [], "notes": notes,
        })

    # ------------------------------------------------------------- base entities
    def build_customers(self) -> None:
        n = self.cfg.n_customers
        rng = self.rng
        ids = [f"CUST-{10000 + i}" for i in range(n)]
        is_business = rng.random(n) < 0.10
        seg_ind = self._choice_weighted(ref.INDIVIDUAL_SEGMENT_WEIGHTS, n)
        seg_bus = self._choice_weighted(ref.BUSINESS_SEGMENT_WEIGHTS, n)
        segment = np.where(is_business, seg_bus, seg_ind)
        city_names = [c[0] for c in ref.CITIES]
        city_w = np.array([c[4] for c in ref.CITIES])
        cities = rng.choice(city_names, size=n, p=city_w / city_w.sum())
        created = [self.as_of - pd.Timedelta(days=int(d)) for d in rng.integers(400, 3000, size=n)]
        risk = rng.choice(["low", "medium", "high"], size=n, p=[0.80, 0.17, 0.03])
        rows = []
        for i, cid in enumerate(ids):
            _, country, lat, lon = self._city(cities[i])
            hlat, hlon = self._jitter(lat, lon, 0.05)
            rows.append({
                "customer_id": cid, "customer_type": "business" if is_business[i] else "individual",
                "segment": segment[i], "created_at": created[i], "country": country,
                "home_city": cities[i], "home_latitude": hlat, "home_longitude": hlon,
                "risk_profile": risk[i], "status": "active", "kyc_level": int(rng.integers(1, 4)),
            })
        self.customers = pd.DataFrame(rows)
        self.cust_index = {r["customer_id"]: r for r in rows}

        # identifiers: phone, email, address (hashed; masked display only)
        ident = []
        household_partner: dict[str, str] = {}
        individuals = self.customers[self.customers.customer_type == "individual"]
        by_city = individuals.groupby("home_city").customer_id.apply(list).to_dict()
        for members in by_city.values():
            members = list(members)
            rng.shuffle(members)
            k = int(len(members) * 0.04) // 2 * 2
            for a, b in zip(members[0:k:2], members[1:k:2]):
                household_partner[a] = b
                household_partner[b] = a
        self.household_partner = household_partner
        for r in rows:
            cid = r["customer_id"]
            phone = f"+{rng.integers(10**10, 10**11)}"
            email = f"user{cid[5:]}@example.com"
            addr_owner = min(cid, household_partner.get(cid, cid))
            address = f"address-of-{addr_owner}"
            for t, v, masked in (
                ("phone", phone, f"***{phone[-4:]}"),
                ("email", email, f"{email[0]}***@example.com"),
                ("address", address, f"*** {r['home_city']}"),
            ):
                ident.append({"customer_id": cid, "identifier_type": t,
                              "value_hash": _hash(v, self.cfg.hash_salt), "masked_value": masked})
        self.identifiers = pd.DataFrame(ident)

    def build_accounts(self) -> None:
        rng = self.rng
        rows = []
        n_acc = 0
        self.primary_account: dict[str, str] = {}
        self.accounts_of: dict[str, list[str]] = {}
        for r in self.customers.itertuples():
            cur = ref.CURRENCY_BY_COUNTRY[r.country]
            plan = [("current" if r.customer_type == "business" else "current", cur)]
            if r.customer_type == "individual" and rng.random() < 0.55:
                plan.append(("savings", cur))
            if r.segment == "affluent" and rng.random() < 0.25 and cur != "USD":
                plan.append(("domiciliary", "USD"))
            if r.customer_type == "business" and rng.random() < 0.35:
                plan.append(("current", cur))
            for j, (atype, c) in enumerate(plan):
                aid = f"ACC-{200000 + n_acc}"
                n_acc += 1
                opened = r.created_at + pd.Timedelta(days=int(rng.integers(0, 60)))
                opened = min(opened, self.history_start - pd.Timedelta(days=30))
                rows.append({"account_id": aid, "customer_id": r.customer_id, "account_type": atype,
                             "currency": c, "opened_at": opened, "status": "active",
                             "opening_balance": round(float(rng.lognormal(6.0, 1.2)) * ref.FX_PER_USD[c], 2)})
                self.accounts_of.setdefault(r.customer_id, []).append(aid)
                if j == 0:
                    self.primary_account[r.customer_id] = aid
        self.accounts = pd.DataFrame(rows)
        self.acc_index = {r["account_id"]: r for r in rows}

    def build_merchants(self, n: int = 2000) -> None:
        rng = self.rng
        cats = self._choice_weighted({k: float(v) for k, v in ref.MERCHANT_CATEGORY_WEIGHTS.items()}, n)
        city_names = [c[0] for c in ref.CITIES]
        city_w = np.array([c[4] for c in ref.CITIES])
        cities = rng.choice(city_names, size=n, p=city_w / city_w.sum())
        rows = []
        for i in range(n):
            _, country, lat, lon = self._city(cities[i])
            mlat, mlon = self._jitter(lat, lon, 0.06)
            base = ref.MERCHANT_CATEGORIES[cats[i]][0]
            if base == "low" and rng.random() < 0.08:
                base = "medium"
            name = f"{rng.choice(ref.MERCHANT_NAME_PARTS[0])} {rng.choice(ref.MERCHANT_NAME_PARTS[1])}"
            rows.append({"merchant_id": f"MER-{5000 + i}", "name": name, "category": cats[i],
                         "country": country, "city": cities[i], "latitude": mlat,
                         "longitude": mlon, "risk_profile": base})
        self.merchants = pd.DataFrame(rows)
        self.merchants_by_city = {
            c: g for c, g in self.merchants.groupby("city")
        }
        self.city_cat_merchants: dict[tuple[str, str], list[Any]] = {}
        for m in self.merchants.itertuples(index=False):
            self.city_cat_merchants.setdefault((m.city, m.category), []).append(m)
            self.city_cat_merchants.setdefault((m.city, "*"), []).append(m)
            self.city_cat_merchants.setdefault(("*", m.category), []).append(m)

    def build_devices(self) -> None:
        rng = self.rng
        rows = []
        self.devices_of: dict[str, list[str]] = {}
        self.device_ip: dict[str, str] = {}
        n_dev = 0

        def new_device(country: str, first_seen: pd.Timestamp, dtype: str | None = None) -> str:
            nonlocal n_dev
            did = f"DEV-{300000 + n_dev}"
            n_dev += 1
            dtype = dtype or str(rng.choice(["android", "ios", "web_browser"], p=[0.55, 0.25, 0.20]))
            rows.append({"device_id": did, "device_type": dtype,
                         "fingerprint": hashlib.sha256(f"fp-{did}-{self.cfg.seed}".encode()).hexdigest()[:32],
                         "first_seen": first_seen, "last_seen": first_seen})
            self.device_ip[did] = self._ip_for_country(country)
            return did

        self._new_device = new_device
        for r in self.customers.itertuples():
            cid = r.customer_id
            if cid in self.devices_of:
                continue
            k = 1 + int(rng.random() < 0.25) + (int(rng.integers(1, 3)) if r.customer_type == "business" else 0)
            first = max(r.created_at, self.history_start - pd.Timedelta(days=200))
            devs = [new_device(r.country, first) for _ in range(k)]
            self.devices_of[cid] = devs
            partner = self.household_partner.get(cid)
            if partner and partner not in self.devices_of:
                # households legitimately share one device (false-positive trap for naive rules)
                own = new_device(r.country, first)
                self.devices_of[partner] = [own, devs[0]]
        self.devices = rows  # finalised later (last_seen)

    def build_contacts(self) -> None:
        rng = self.rng
        cust = self.customers
        by_city = cust.groupby("home_city").customer_id.apply(list).to_dict()
        all_ids = cust.customer_id.tolist()
        self.contacts: dict[str, list[str]] = {}
        for r in cust.itertuples():
            k = int(rng.integers(3, 9)) if r.customer_type == "individual" else int(rng.integers(6, 15))
            local = by_city[r.home_city]
            picks: set[str] = set()
            while len(picks) < k:
                pool = local if rng.random() < 0.75 and len(local) > 10 else all_ids
                c = pool[int(rng.integers(0, len(pool)))]
                if c != r.customer_id:
                    picks.add(self.primary_account[c])
            self.contacts[r.customer_id] = sorted(picks)

    # --------------------------------------------------------- base transactions
    def build_base_transactions(self, dormant: set[str], hf_business: set[str]) -> None:
        rng = self.rng
        mix_cache = {k: (list(v), np.array(list(v.values()))) for k, v in ref.OUTBOUND_TYPE_MIX.items()}
        cat_names = list(ref.MERCHANT_CATEGORY_WEIGHTS)
        everyday = [c for c in cat_names if ref.MERCHANT_CATEGORIES[c][0] != "high"
                    and c not in ("real_estate", "education", "travel")]
        for r in self.customers.itertuples():
            cid = r.customer_id
            rate, med_usd, sigma, income = ref.SEGMENTS[r.segment]
            activity = float(rng.lognormal(0, 0.45))
            if cid in hf_business:
                rate, activity = 1.5, 1.0  # consistently very active, throughout history
            start = self.history_start
            end = self.as_of
            if cid in dormant:
                end = self.window_start - pd.Timedelta(days=int(rng.integers(130, 150)))
            days = max((end - start).days, 0)
            n = int(rng.poisson(rate * activity * days))
            if n == 0 and days == 0:
                continue
            acc = self.primary_account[cid]
            cur = self.acc_index[acc]["currency"]
            devs = self.devices_of[cid]
            types, probs = mix_cache[r.customer_type]
            ttypes = rng.choice(types, size=n, p=probs)
            secs = rng.uniform(0, max(days, 1) * 86400, size=n)
            hours = rng.choice(np.arange(24), size=n, p=_HOUR_P)
            for i in range(n):
                day0 = start + pd.Timedelta(seconds=float(secs[i]))
                ts = day0.normalize() + pd.Timedelta(hours=int(hours[i]), minutes=float(rng.uniform(0, 60)))
                if ts >= end:
                    ts = end - pd.Timedelta(minutes=float(rng.uniform(1, 600)))
                t = ttypes[i]
                status = "completed" if rng.random() > 0.015 else str(rng.choice(["failed", "reversed"]))
                if t == "card_payment":
                    if rng.random() < 0.015:
                        cat = str(rng.choice(["gambling", "money_transfer", "jewelry", "crypto_exchange"]))
                    else:
                        cat = everyday[int(rng.integers(0, len(everyday)))]
                    cand = (self.city_cat_merchants.get((r.home_city, cat))
                            or self.city_cat_merchants.get((r.home_city, "*"))
                            or self.city_cat_merchants[("*", cat)])
                    m = cand[int(rng.integers(0, len(cand)))]
                    mmed, msig = ref.MERCHANT_CATEGORIES[m.category][1:]
                    usd = float(rng.lognormal(np.log(mmed * (1.5 if r.segment == "affluent" else 1.0)), msig))
                    online = rng.random() < 0.3
                    dev = devs[int(rng.integers(0, len(devs)))] if online else None
                    self._add_txn(timestamp=ts, sender_account_id=acc, merchant_id=m.merchant_id,
                                  amount=self._usd_to(usd, cur), currency=cur,
                                  transaction_type="card_payment", channel="web" if online else "pos",
                                  country=m.country, latitude=m.latitude, longitude=m.longitude,
                                  device_id=dev, ip_address=self.device_ip[dev] if dev else None,
                                  status=status)
                elif t == "transfer":
                    usd = float(rng.lognormal(np.log(med_usd), sigma))
                    dev = devs[int(rng.integers(0, len(devs)))]
                    lat, lon = self._jitter(r.home_latitude, r.home_longitude, 0.03)
                    external = rng.random() < 0.2
                    receiver = None if external else self.contacts[cid][int(rng.integers(0, len(self.contacts[cid])))]
                    self._add_txn(timestamp=ts, sender_account_id=acc, receiver_account_id=receiver,
                                  amount=self._usd_to(usd, cur), currency=cur, transaction_type="transfer",
                                  channel=str(rng.choice(["mobile", "web"], p=[0.75, 0.25])),
                                  country=r.country, latitude=lat, longitude=lon, device_id=dev,
                                  ip_address=self.device_ip[dev], status=status,
                                  external_counterparty=f"EXT-{int(rng.integers(1000, 9999))}" if external else None)
                elif t == "bill_payment":
                    bcat = "utilities" if rng.random() < 0.5 else "telecom"
                    cand = (self.city_cat_merchants.get((r.home_city, bcat))
                            or self.city_cat_merchants[("*", bcat)])
                    m = cand[int(rng.integers(0, len(cand)))]
                    usd = float(rng.lognormal(np.log(ref.MERCHANT_CATEGORIES[m.category][1]), 0.5))
                    dev = devs[0]
                    self._add_txn(timestamp=ts, sender_account_id=acc, merchant_id=m.merchant_id,
                                  amount=self._usd_to(usd, cur), currency=cur, transaction_type="bill_payment",
                                  channel="mobile", country=r.country, latitude=r.home_latitude,
                                  longitude=r.home_longitude, device_id=dev, ip_address=self.device_ip[dev],
                                  status=status)
                else:  # cash withdrawal
                    usd = float(rng.lognormal(np.log(max(med_usd * 0.6, 20)), 0.6))
                    lat, lon = self._jitter(r.home_latitude, r.home_longitude, 0.03)
                    self._add_txn(timestamp=ts, sender_account_id=acc, amount=self._usd_to(usd, cur),
                                  currency=cur, transaction_type="cash_withdrawal", channel="atm",
                                  country=r.country, latitude=lat, longitude=lon, status=status)
            # inbound income
            if r.customer_type == "individual" and income > 0 and r.segment != "student":
                month = start.normalize().replace(day=25)
                if month < start:
                    month = month + pd.offsets.MonthBegin(1) + pd.Timedelta(days=24)
                employer = f"EMPLOYER-{int(rng.integers(100, 999))}"
                while month < end:
                    usd = income * float(rng.normal(1.0, 0.03))
                    self._add_txn(timestamp=month + pd.Timedelta(hours=9), receiver_account_id=acc,
                                  amount=self._usd_to(usd, cur), currency=cur, transaction_type="salary",
                                  channel="api", country=r.country, external_counterparty=employer)
                    month = (month + pd.offsets.MonthBegin(1)) + pd.Timedelta(days=24)
            elif r.customer_type == "individual":
                k = int(rng.poisson(days / 20))
                for _ in range(k):
                    usd = float(rng.lognormal(np.log(60), 0.5))
                    self._add_txn(timestamp=self._ts(start, end), receiver_account_id=acc,
                                  amount=self._usd_to(usd, cur), currency=cur, transaction_type="deposit",
                                  channel="mobile", country=r.country,
                                  external_counterparty=f"EXT-{int(rng.integers(1000, 9999))}")
            else:
                k = int(rng.poisson(rate * activity * days * 0.5))
                for _ in range(k):
                    usd = float(rng.lognormal(np.log(med_usd * 1.4), sigma))
                    self._add_txn(timestamp=self._ts(start, end), receiver_account_id=acc,
                                  amount=self._usd_to(usd, cur), currency=cur, transaction_type="deposit",
                                  channel=str(rng.choice(["api", "branch", "web"])), country=r.country,
                                  external_counterparty=f"EXT-{int(rng.integers(1000, 9999))}")

    # ---------------------------------------------------------------- scenarios
    def _cust_ctx(self, cid: str) -> tuple[Any, str, str, list[str]]:
        r = self.cust_index[cid]
        acc = self.primary_account[cid]
        return r, acc, self.acc_index[acc]["currency"], self.devices_of[cid]

    def _foreign_city(self, home_country: str) -> tuple[str, str, float, float]:
        options = [c for c in ref.CITIES if c[1] != home_country]
        c = options[int(self.rng.integers(0, len(options)))]
        return c[0], c[1], c[2], c[3]

    def inject_account_takeover(self, cids: list[str]) -> None:
        rng = self.rng
        for cid in cids:
            r, acc, cur, _ = self._cust_ctx(cid)
            city, country, lat, lon = self._foreign_city(r["country"])
            t0 = self._ts(self.window_start + pd.Timedelta(days=3), self.as_of - pd.Timedelta(days=2))
            dev = self._new_device(country, t0, "web_browser")
            ip = self._ip_for_country(country)
            self.device_ip[dev] = ip
            n = int(rng.integers(6, 13))
            seg_med = ref.SEGMENTS[r["segment"]][1]
            ext = [f"EXT-ATO-{int(rng.integers(10000, 99999))}" for _ in range(3)]
            for i in range(n):
                ts = t0 + pd.Timedelta(minutes=float(i * rng.uniform(4, 12)))
                usd = float(seg_med * rng.uniform(2.0, 5.0))
                self._add_txn(timestamp=ts, sender_account_id=acc, amount=self._usd_to(usd, cur), currency=cur,
                              transaction_type="transfer", channel="web", country=country,
                              latitude=self._jitter(lat, lon)[0], longitude=self._jitter(lat, lon)[1],
                              device_id=dev, ip_address=ip, external_counterparty=ext[i % 3])
            self._label(cid, "account_takeover", [dev], notes=f"takeover from {city}")

    def inject_mules(self, cids: list[str], all_ids: list[str]) -> None:
        rng = self.rng
        for cid in cids:
            r, acc, cur, devs = self._cust_ctx(cid)
            t0 = self._ts(self.window_start + pd.Timedelta(days=2), self.as_of - pd.Timedelta(days=6))
            senders = rng.choice(all_ids, size=int(rng.integers(10, 22)), replace=False)
            total_usd = 0.0
            for j, s in enumerate(senders):
                if s == cid:
                    continue
                s_acc = self.primary_account[s]
                s_cur = self.acc_index[s_acc]["currency"]
                usd = float(rng.uniform(150, 900))
                total_usd += usd
                ts = t0 + pd.Timedelta(hours=float(j * rng.uniform(1, 5)))
                sd = self.devices_of[s][0]
                sr = self.cust_index[s]
                self._add_txn(timestamp=ts, sender_account_id=s_acc, receiver_account_id=acc,
                              amount=self._usd_to(usd, s_cur), currency=s_cur, transaction_type="transfer",
                              channel="mobile", country=sr["country"], latitude=sr["home_latitude"],
                              longitude=sr["home_longitude"], device_id=sd, ip_address=self.device_ip[sd])
                # forward most of it out within a few hours
                out_usd = usd * float(rng.uniform(0.88, 0.97))
                self._add_txn(timestamp=ts + pd.Timedelta(hours=float(rng.uniform(0.3, 6))),
                              sender_account_id=acc, amount=self._usd_to(out_usd, cur), currency=cur,
                              transaction_type="transfer", channel="mobile", country=r["country"],
                              latitude=r["home_latitude"], longitude=r["home_longitude"],
                              device_id=devs[0], ip_address=self.device_ip[devs[0]],
                              external_counterparty=f"EXT-MULE-{int(rng.integers(100, 140))}")
            self._label(cid, "mule_account", notes=f"fan-in from {len(senders)} senders, ~USD {total_usd:,.0f}")

    def inject_device_rings(self, rings: list[list[str]]) -> None:
        rng = self.rng
        for ring in rings:
            r0 = self.cust_index[ring[0]]
            t0 = self._ts(self.window_start + pd.Timedelta(days=1), self.as_of - pd.Timedelta(days=8))
            shared = self._new_device(r0["country"], t0, "android")
            ip = self.device_ip[shared]
            sink = f"EXT-RING-{int(rng.integers(1000, 9999))}"
            for k, cid in enumerate(ring):
                r, acc, cur, _ = self._cust_ctx(cid)
                nxt = self.primary_account[ring[(k + 1) % len(ring)]]
                for j in range(int(rng.integers(3, 6))):
                    ts = t0 + pd.Timedelta(hours=float(k * 7 + j * rng.uniform(1, 30)))
                    usd = float(rng.uniform(80, 400))
                    target_ext = rng.random() < 0.4
                    self._add_txn(timestamp=ts, sender_account_id=acc,
                                  receiver_account_id=None if target_ext else nxt,
                                  amount=self._usd_to(usd, cur), currency=cur, transaction_type="transfer",
                                  channel="mobile", country=r["country"], latitude=r["home_latitude"],
                                  longitude=r["home_longitude"], device_id=shared, ip_address=ip,
                                  external_counterparty=sink if target_ext else None)
                self._label(cid, "device_sharing_ring", [shared] + [c for c in ring if c != cid])

    def inject_bursts(self, cids: list[str]) -> None:
        rng = self.rng
        online = self.merchants[self.merchants.category.isin(["electronics", "clothing", "telecom", "gambling"])]
        for cid in cids:
            r, acc, cur, devs = self._cust_ctx(cid)
            t0 = self._ts(self.window_start + pd.Timedelta(days=2), self.as_of - pd.Timedelta(days=1))
            n = int(rng.integers(18, 35))
            for i in range(n):
                m = online.iloc[int(rng.integers(0, len(online)))]
                ts = t0 + pd.Timedelta(seconds=float(i * rng.uniform(30, 150)))
                status = "failed" if rng.random() < 0.3 else "completed"
                self._add_txn(timestamp=ts, sender_account_id=acc, merchant_id=m.merchant_id,
                              amount=self._usd_to(float(rng.uniform(1, 15)), cur), currency=cur,
                              transaction_type="card_payment", channel="web", country=m.country,
                              latitude=m.latitude, longitude=m.longitude, device_id=devs[0],
                              ip_address=self.device_ip[devs[0]], status=status)
            self._label(cid, "transaction_burst", notes=f"{n} card-not-present payments in <1.5h")

    def inject_cycles(self, cycles: list[list[str]]) -> None:
        rng = self.rng
        for cyc in cycles:
            t0 = self._ts(self.window_start + pd.Timedelta(days=2), self.as_of - pd.Timedelta(days=5))
            usd = float(rng.uniform(3000, 12000))
            for rounds in range(2):
                for k, cid in enumerate(cyc):
                    r, acc, cur, devs = self._cust_ctx(cid)
                    nxt = self.primary_account[cyc[(k + 1) % len(cyc)]]
                    ts = t0 + pd.Timedelta(hours=float(rounds * 40 + k * rng.uniform(3, 10)))
                    usd_k = usd * (0.985 ** (k + rounds * len(cyc)))
                    self._add_txn(timestamp=ts, sender_account_id=acc, receiver_account_id=nxt,
                                  amount=self._usd_to(usd_k, cur), currency=cur, transaction_type="transfer",
                                  channel="web", country=r["country"], latitude=r["home_latitude"],
                                  longitude=r["home_longitude"], device_id=devs[0],
                                  ip_address=self.device_ip[devs[0]])
            for cid in cyc:
                self._label(cid, "circular_transfer", [c for c in cyc if c != cid])

    def inject_impossible_travel(self, cids: list[str]) -> None:
        rng = self.rng
        for cid in cids:
            r, acc, cur, _ = self._cust_ctx(cid)
            t0 = self._ts(self.window_start + pd.Timedelta(days=1), self.as_of - pd.Timedelta(days=1))
            home_m = self.merchants_by_city[r["home_city"]].iloc[0]
            fc, fcountry, flat, flon = self._foreign_city(r["country"])
            far_m = self.merchants_by_city[fc]
            # ensure the jump is genuinely far
            while _haversine(r["home_latitude"], r["home_longitude"], flat, flon) < 2500:
                fc, fcountry, flat, flon = self._foreign_city(r["country"])
                far_m = self.merchants_by_city[fc]
            fm = far_m.iloc[int(rng.integers(0, len(far_m)))]
            self._add_txn(timestamp=t0, sender_account_id=acc, merchant_id=home_m.merchant_id,
                          amount=self._usd_to(float(rng.uniform(20, 60)), cur), currency=cur,
                          transaction_type="card_payment", channel="pos", country=home_m.country,
                          latitude=home_m.latitude, longitude=home_m.longitude)
            for j in range(int(rng.integers(2, 4))):
                self._add_txn(timestamp=t0 + pd.Timedelta(minutes=float(rng.uniform(40, 110) + 20 * j)),
                              sender_account_id=acc, merchant_id=fm.merchant_id,
                              amount=self._usd_to(float(rng.uniform(150, 700)), cur), currency=cur,
                              transaction_type="card_payment", channel="pos", country=fm.country,
                              latitude=fm.latitude, longitude=fm.longitude)
            self._label(cid, "geographic_anomaly", notes=f"card present in {r['home_city']} and {fc} within 2h")

    def inject_dormant(self, cids: list[str]) -> None:
        rng = self.rng
        for cid in cids:
            r, acc, cur, devs = self._cust_ctx(cid)
            t0 = self._ts(self.window_start + pd.Timedelta(days=3), self.as_of - pd.Timedelta(days=3))
            usd = float(rng.uniform(8000, 30000))
            self._add_txn(timestamp=t0, receiver_account_id=acc, amount=self._usd_to(usd, cur), currency=cur,
                          transaction_type="deposit", channel="branch", country=r["country"],
                          external_counterparty=f"EXT-{int(rng.integers(1000, 9999))}")
            for _ in range(int(rng.integers(2, 5))):
                self._add_txn(timestamp=t0 + pd.Timedelta(hours=float(rng.uniform(2, 30))),
                              sender_account_id=acc, amount=self._usd_to(usd * float(rng.uniform(0.2, 0.3)), cur),
                              currency=cur, transaction_type="transfer", channel="mobile", country=r["country"],
                              latitude=r["home_latitude"], longitude=r["home_longitude"], device_id=devs[0],
                              ip_address=self.device_ip[devs[0]],
                              external_counterparty=f"EXT-{int(rng.integers(1000, 9999))}")
            self._label(cid, "dormant_reactivation", notes=f"inactive ~{self.cfg.window_days + 130}+ days")

    def inject_high_risk_merchant(self, cids: list[str]) -> None:
        rng = self.rng
        hr = self.merchants[self.merchants.risk_profile == "high"]
        for cid in cids:
            r, acc, cur, devs = self._cust_ctx(cid)
            ms = hr.iloc[rng.choice(len(hr), size=2, replace=False)]
            n = int(rng.integers(8, 15))
            for i in range(n):
                m = ms.iloc[i % 2]
                ts = self._ts(self.window_start, self.as_of)
                usd = float(rng.uniform(100, 300) * (1 + i / 4))
                self._add_txn(timestamp=ts, sender_account_id=acc, merchant_id=m.merchant_id,
                              amount=self._usd_to(usd, cur), currency=cur, transaction_type="card_payment",
                              channel="web", country=m.country, latitude=m.latitude, longitude=m.longitude,
                              device_id=devs[0], ip_address=self.device_ip[devs[0]])
            self._label(cid, "high_risk_merchant", list(ms.merchant_id))

    def inject_legit_high_value(self, cids: list[str]) -> None:
        rng = self.rng
        cats = self.merchants[self.merchants.category.isin(["real_estate", "education", "travel"])]
        for cid in cids:
            r, acc, cur, devs = self._cust_ctx(cid)
            local = cats[cats.country == r["country"]]
            m = (local if not local.empty else cats).iloc[0]
            usd = ref.SEGMENTS[r["segment"]][1] * float(rng.uniform(15, 30))
            self._add_txn(timestamp=self._ts(self.window_start, self.as_of), sender_account_id=acc,
                          merchant_id=m.merchant_id, amount=self._usd_to(usd, cur), currency=cur,
                          transaction_type="card_payment", channel="web", country=m.country,
                          latitude=m.latitude, longitude=m.longitude, device_id=devs[0],
                          ip_address=self.device_ip[devs[0]])
            self._label(cid, "legit_high_value", notes=f"one-off {m.category} payment")

    def inject_travel(self, cids: list[str]) -> None:
        rng = self.rng
        for cid in cids:
            r, acc, cur, devs = self._cust_ctx(cid)
            fc, fcountry, flat, flon = self._foreign_city(r["country"])
            dist = _haversine(r["home_latitude"], r["home_longitude"], flat, flon)
            t0 = self._ts(self.window_start + pd.Timedelta(days=2), self.as_of - pd.Timedelta(days=10))
            # last home transaction, then a realistic flight
            home_m = self.merchants_by_city[r["home_city"]].iloc[0]
            self._add_txn(timestamp=t0, sender_account_id=acc, merchant_id=home_m.merchant_id,
                          amount=self._usd_to(30.0, cur), currency=cur, transaction_type="card_payment",
                          channel="pos", country=home_m.country, latitude=home_m.latitude,
                          longitude=home_m.longitude)
            arrive = t0 + pd.Timedelta(hours=dist / 650 + 4)
            fm_all = self.merchants_by_city[fc]
            for j in range(int(rng.integers(4, 8))):
                fm = fm_all.iloc[int(rng.integers(0, len(fm_all)))]
                self._add_txn(timestamp=arrive + pd.Timedelta(hours=float(j * rng.uniform(5, 20))),
                              sender_account_id=acc, merchant_id=fm.merchant_id,
                              amount=self._usd_to(float(rng.lognormal(np.log(40), 0.6)), cur), currency=cur,
                              transaction_type="card_payment", channel="pos", country=fm.country,
                              latitude=fm.latitude, longitude=fm.longitude)
            self._label(cid, "travel_legit", notes=f"trip to {fc}")

    # -------------------------------------------------------- alerts & history
    def build_alerts_and_investigations(self) -> None:
        rng = self.rng
        cust_ids = self.customers.customer_id.tolist()
        alerts = []
        invs = []
        n_al = 0
        suspicious = [lb["entity_id"] for lb in self.labels if lb["is_suspicious"]]
        # Historical legacy-rule alerts, independent of ground truth (noise by design).
        hist = rng.choice(cust_ids, size=max(5, len(cust_ids) // 40), replace=False)
        for cid in hist:
            ts = self._ts(self.history_start, self.window_start - pd.Timedelta(days=1))
            alerts.append({"alert_id": f"ALR-{n_al:06d}", "entity_id": cid, "entity_type": "customer",
                           "alert_type": str(rng.choice(["LEGACY_LARGE_CASH", "LEGACY_VELOCITY", "LEGACY_GEO"])),
                           "severity": str(rng.choice(["low", "medium"])), "risk_score": float(rng.uniform(20, 60)),
                           "created_at": ts, "status": str(rng.choice(["closed_false_positive", "closed_no_action"]))})
            n_al += 1
        # Current triage queue: a legacy rule fires for some suspicious customers and some noise.
        queue = list(rng.choice(suspicious, size=len(suspicious) // 2, replace=False)) if suspicious else []
        queue += list(rng.choice(cust_ids, size=max(3, len(cust_ids) // 70), replace=False))
        for cid in queue:
            ts = self._ts(self.window_start, self.as_of)
            alerts.append({"alert_id": f"ALR-{n_al:06d}", "entity_id": cid, "entity_type": "customer",
                           "alert_type": str(rng.choice(["LEGACY_VELOCITY", "LEGACY_LARGE_TRANSFER", "LEGACY_GEO"])),
                           "severity": str(rng.choice(["medium", "high"], p=[0.7, 0.3])),
                           "risk_score": float(rng.uniform(40, 85)), "created_at": ts, "status": "open"})
            n_al += 1
        self.alerts = pd.DataFrame(alerts)

        # Historical closed investigations with analyst conclusions (episodic memory seed).
        hist_inv = rng.choice(cust_ids, size=max(5, len(cust_ids) // 50), replace=False)
        signal_pool = ["VELOCITY_SPIKE", "NEW_DEVICE", "AMOUNT_DEVIATION", "GEO_NEW_COUNTRY", "DEVICE_SHARING",
                       "HIGH_RISK_MERCHANT"]
        for i, cid in enumerate(hist_inv):
            created = self._ts(self.history_start, self.window_start - pd.Timedelta(days=5))
            sigs = [str(x) for x in rng.choice(signal_pool, size=int(rng.integers(1, 3)), replace=False)]
            conclusion = str(rng.choice(["legitimate", "insufficient_evidence", "confirmed_suspicious"],
                                        p=[0.65, 0.25, 0.10]))
            invs.append({"investigation_id": f"INV-H{i:05d}", "subject_id": cid, "subject_type": "customer",
                         "assigned_to": f"analyst{int(rng.integers(1, 6))}", "status": "closed",
                         "created_at": created, "closed_at": created + pd.Timedelta(days=int(rng.integers(1, 15))),
                         "conclusion": conclusion, "signals": json.dumps(sigs),
                         "summary": f"Historical review triggered by {', '.join(sigs)}; analyst conclusion: {conclusion}."})
        self.investigations = pd.DataFrame(invs)

    # ------------------------------------------------------------------ finalise
    def finalise(self) -> None:
        tx = pd.DataFrame(self.txn_rows, columns=TXN_COLUMNS)
        tx["timestamp"] = pd.to_datetime(tx["timestamp"], utc=True)
        tx = tx.sort_values("timestamp", kind="mergesort").reset_index(drop=True)
        tx.insert(0, "transaction_id", [f"TXN-{i:08d}" for i in range(len(tx))])
        self.transactions = tx

        dev = pd.DataFrame(self.devices)
        used = tx.dropna(subset=["device_id"]).groupby("device_id").timestamp.agg(["min", "max"])
        dev = dev.set_index("device_id")
        dev["first_seen"] = pd.to_datetime(dev["first_seen"], utc=True)
        dev["last_seen"] = pd.to_datetime(dev["last_seen"], utc=True)
        dev.loc[used.index, "first_seen"] = np.minimum(dev.loc[used.index, "first_seen"], used["min"])
        dev.loc[used.index, "last_seen"] = used["max"]
        self.devices_df = dev.reset_index()

        acc = self.accounts.copy()
        done = tx[tx.status == "completed"]
        out = done.groupby("sender_account_id").amount.sum()
        inn = done.groupby("receiver_account_id").amount.sum()
        bal = acc.set_index("account_id")["opening_balance"].add(inn, fill_value=0).sub(out, fill_value=0)
        acc["balance"] = acc.account_id.map(bal).round(2)
        # if simulated balance is negative, assume an overdraft facility was repaid: floor at a small positive.
        acc.loc[acc.balance < 0, "balance"] = acc.loc[acc.balance < 0, "balance"].abs().mul(0.05).round(2)
        self.accounts = acc.drop(columns=["opening_balance"])

    def generate(self) -> None:
        cfg = self.cfg
        self.build_customers()
        self.build_accounts()
        self.build_merchants(2000 if cfg.n_customers >= 10_000 else max(200, int(2000 * cfg.n_customers / 10_000)))
        self.build_devices()
        self.build_contacts()
        cust = self.customers
        individuals = cust[cust.customer_type == "individual"].customer_id.tolist()
        businesses = cust[cust.customer_type == "business"].customer_id.tolist()
        # exclude household members from scenario selection to keep labels clean
        individuals = [c for c in individuals if c not in self.household_partner]
        self.rng.shuffle(individuals)

        hf = set(self._pick_unused(businesses, cfg.scaled("high_frequency_legit")))
        dormant = set(self._pick_unused(individuals, cfg.scaled("dormant_reactivation")))
        # a dormant account must not keep receiving everyday transfers from contacts
        dormant_accounts = {a for c in dormant for a in self.accounts_of[c]}
        for cid, lst in self.contacts.items():
            kept = [a for a in lst if a not in dormant_accounts]
            self.contacts[cid] = kept or [self.primary_account[c] for c in individuals[:3] if c not in dormant]
        self.build_base_transactions(dormant=dormant, hf_business=hf)
        for c in hf:
            self._label(c, "high_frequency_legit", notes="consistently high-volume business")

        all_ids = [c for c in cust.customer_id.tolist() if c not in dormant]
        self.inject_account_takeover(self._pick_unused(individuals, cfg.scaled("account_takeover")))
        self.inject_mules(self._pick_unused(individuals, cfg.scaled("mule_account")), all_ids)
        rings = [self._pick_unused(individuals, int(self.rng.integers(4, 7)))
                 for _ in range(cfg.scaled("device_sharing_ring"))]
        self.inject_device_rings(rings)
        self.inject_bursts(self._pick_unused(individuals, cfg.scaled("transaction_burst")))
        cycles = [self._pick_unused(individuals, int(self.rng.integers(3, 5)))
                  for _ in range(cfg.scaled("circular_transfer"))]
        self.inject_cycles(cycles)
        self.inject_impossible_travel(self._pick_unused(individuals, cfg.scaled("geographic_anomaly")))
        self.inject_dormant(sorted(dormant))
        self.inject_high_risk_merchant(self._pick_unused(individuals, cfg.scaled("high_risk_merchant")))
        self.inject_legit_high_value(self._pick_unused(individuals, cfg.scaled("legit_high_value")))
        self.inject_travel(self._pick_unused(individuals, cfg.scaled("travel_legit")))
        normals = self._pick_unused(individuals + businesses, min(cfg.n_normal_labels, len(individuals) // 4))
        for c in normals:
            self._label(c, "normal")
        self.build_alerts_and_investigations()
        self.finalise()

    def write(self, out: Path) -> dict[str, Any]:
        out.mkdir(parents=True, exist_ok=True)
        tables = {
            "customers": self.customers, "customer_identifiers": self.identifiers,
            "accounts": self.accounts, "merchants": self.merchants, "devices": self.devices_df,
            "transactions": self.transactions, "alerts": self.alerts,
            "historical_investigations": self.investigations,
        }
        for name, df in tables.items():
            df.to_csv(out / f"{name}.csv.gz", index=False, compression="gzip")
        (out / "scenario_labels.json").write_text(json.dumps(self.labels, indent=1), encoding="utf-8")
        manifest = {
            "generator": "fira-synthetic", "version": 1, "seed": self.cfg.seed,
            "as_of": self.as_of.isoformat(), "history_start": self.history_start.isoformat(),
            "window_start": self.window_start.isoformat(), "window_days": self.cfg.window_days,
            "counts": {k: int(len(v)) for k, v in tables.items()},
            "scenarios": pd.Series([lb["scenario"] for lb in self.labels]).value_counts().to_dict(),
            "synthetic": True,
        }
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
        return manifest


_HOUR_W = np.array([1, 0.5, 0.3, 0.3, 0.4, 0.8, 2, 4, 6, 7, 7, 7, 8, 8, 7, 7, 7, 7, 8, 8, 7, 5, 3, 2], dtype=float)
_HOUR_P = _HOUR_W / _HOUR_W.sum()


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return float(2 * r * np.arcsin(np.sqrt(a)))


def generate_dataset(out: Path, n_customers: int = 10_000, seed: int = 42,
                     history_days: int = 180) -> dict[str, Any]:
    cfg = GeneratorConfig(n_customers=n_customers, seed=seed, history_days=history_days)
    bank = SyntheticBank(cfg)
    bank.generate()
    return bank.write(out)


def main() -> None:
    p = argparse.ArgumentParser(description="Generate the FIRA synthetic dataset")
    p.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[3] / "data" / "seeds")
    p.add_argument("--customers", type=int, default=10_000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--history-days", type=int, default=180)
    a = p.parse_args()
    manifest = generate_dataset(a.out, a.customers, a.seed, a.history_days)
    print(json.dumps(manifest, indent=2, default=str))


if __name__ == "__main__":
    main()
