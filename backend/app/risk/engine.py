"""Deterministic risk engine.

For an entity and an investigation window it:
  1. loads the customer's transactions for [baseline_start, window_end]
  2. computes baseline and window statistics identically (`period_stats`)
  3. runs each configured detector, which returns a `RiskSignal` only when its
     observed value crosses a threshold derived from config and/or baseline
  4. converts fired signals to point contributions (weight x strength), applies
     group caps for correlated signals, and caps the total.

Detectors record their raw metrics (observed, threshold) even when they do not
fire; these feed the optional ML anomaly model and the evaluation harness.
No LLM is involved anywhere in this module.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from app.analytics.geo import detect_impossible_travel
from app.analytics.stats import (
    inbound_counterparties,
    jensen_shannon,
    max_count_in_window,
    pass_through,
    period_stats,
    split_customer_frame,
)
from app.data.store import DataStore, utcnow
from app.graph.base import GraphBackend
from app.risk.config import RiskConfig, SignalConfig
from app.risk.models import Contribution, EvidenceRef, NotEvaluated, RiskAssessment, RiskSignal


class EntityNotFound(LookupError):
    pass


def strength(observed: float, threshold: float, ramp: float) -> float:
    """0.5 at the threshold, rising linearly to 1.0 at threshold * (1 + ramp)."""
    if threshold <= 0:
        return 1.0
    excess = observed / threshold - 1.0
    return round(0.5 + 0.5 * max(0.0, min(1.0, excess / ramp)), 4)


def severity_of(s: float) -> Any:
    return "high" if s >= 0.85 else "medium" if s >= 0.65 else "low"


def _fmt(x: float, nd: int = 2) -> str:
    return f"{x:,.{nd}f}"


class _Ctx:
    """Everything a detector needs, computed once per assessment."""

    def __init__(self, engine: RiskEngine, customer: Any, accounts: list[Any], window_start: datetime,
                 window_end: datetime, baseline_start: datetime):
        self.engine = engine
        self.store = engine.store
        self.customer = customer
        self.accounts = accounts
        self.account_ids = {a.account_id for a in accounts}
        self.window_start, self.window_end, self.baseline_start = window_start, window_end, baseline_start
        tx = self.store.transactions_for_accounts(sorted(self.account_ids), baseline_start, window_end)
        ws = pd.Timestamp(window_start)
        self.base_tx = tx[tx.timestamp < ws]
        self.win_tx = tx[tx.timestamp >= ws]
        self.base = split_customer_frame(self.base_tx, self.account_ids)
        self.win = split_customer_frame(self.win_tx, self.account_ids)
        self.base_stats = period_stats(self.base, baseline_start, window_start)
        self.win_stats = period_stats(self.win, window_start, window_end)
        self.entity_type = "customer"
        self.entity_id = customer.customer_id
        self.thin_baseline = self.base_stats["n_outbound"] < 5


class RiskEngine:
    def __init__(self, store: DataStore, graph: GraphBackend | None, config: RiskConfig, ml_model: Any = None):
        self.store = store
        self.graph = graph
        self.config = config
        self.ml_model = ml_model
        self.detectors: list[tuple[str, Callable[[_Ctx, SignalConfig], RiskSignal | NotEvaluated | None]]] = [
            ("TRANSACTION_BURST", self._burst),
            ("VELOCITY_SPIKE", self._velocity),
            ("AMOUNT_DEVIATION", self._amount),
            ("PEER_AMOUNT_DEVIATION", self._peer_amount),
            ("RAPID_PASS_THROUGH", self._pass_through),
            ("FAN_IN", self._fan_in),
            ("GEO_NEW_COUNTRY", self._new_country),
            ("IMPOSSIBLE_TRAVEL", self._impossible_travel),
            ("NEW_DEVICE", self._new_device),
            ("DEVICE_SHARING", self._device_sharing),
            ("SHARED_IDENTIFIER", self._shared_identifier),
            ("CIRCULAR_FLOW", self._circular),
            ("DORMANT_REACTIVATION", self._dormant),
            ("HIGH_RISK_MERCHANT", self._high_risk_merchant),
            ("BEHAVIOURAL_SHIFT", self._behaviour_shift),
            ("NETWORK_EXPOSURE", self._network_exposure),
            ("HISTORICAL_ALERTS", self._historical_alerts),
        ]

    # ----------------------------------------------------------------- public
    def assess_customer(self, customer_id: str, window_end: datetime | None = None,
                        lookback_days: int = 30, baseline_days: int = 90,
                        account_ids: list[str] | None = None) -> RiskAssessment:
        customer = self.store.get_customer(customer_id)
        if customer is None:
            raise EntityNotFound(f"customer {customer_id} not found")
        accounts = self.store.accounts_for_customer(customer_id)
        if account_ids is not None:
            accounts = [a for a in accounts if a.account_id in set(account_ids)]
        window_end = window_end or self.store.as_of()
        window_start = window_end - timedelta(days=lookback_days)
        baseline_start = window_start - timedelta(days=baseline_days)
        ctx = _Ctx(self, customer, accounts, window_start, window_end, baseline_start)
        if account_ids is not None and len(account_ids) == 1:
            ctx.entity_type, ctx.entity_id = "account", account_ids[0]
        return self._run(ctx)

    def assess_account(self, account_id: str, window_end: datetime | None = None,
                       lookback_days: int = 30, baseline_days: int = 90) -> RiskAssessment:
        acc = self.store.get_account(account_id)
        if acc is None:
            raise EntityNotFound(f"account {account_id} not found")
        return self.assess_customer(acc.customer_id, window_end, lookback_days, baseline_days, [account_id])

    # --------------------------------------------------------------- pipeline
    def _run(self, ctx: _Ctx) -> RiskAssessment:
        signals: list[RiskSignal] = []
        not_eval: list[NotEvaluated] = []
        metrics: dict[str, dict[str, Any]] = {}
        self._metrics = metrics
        for name, fn in self.detectors:
            sc = self.config.signal(name)
            if sc is None:
                not_eval.append(NotEvaluated(signal_type=name, reason="disabled in configuration"))
                continue
            try:
                res = fn(ctx, sc)
            except Exception as exc:  # a detector bug must not hide the other signals
                not_eval.append(NotEvaluated(signal_type=name, reason=f"detector error: {type(exc).__name__}"))
                continue
            if isinstance(res, NotEvaluated):
                not_eval.append(res)
            elif isinstance(res, RiskSignal):
                signals.append(res)
        ml_sc = self.config.signal("ML_ANOMALY")
        if ml_sc is not None:
            if self.ml_model is None:
                not_eval.append(NotEvaluated(signal_type="ML_ANOMALY", reason="ML model not trained/loaded"))
            else:
                res = self._ml(ctx, ml_sc, metrics)
                if res is not None:
                    signals.append(res)
        contributions, score = self.score(signals)
        dq = []
        if ctx.thin_baseline:
            dq.append(f"Thin baseline: {ctx.base_stats['n_outbound']} outbound transactions in the "
                      f"{(ctx.window_start - ctx.baseline_start).days}-day baseline; deviation signals are less reliable.")
        if ctx.win_stats["n_outbound"] + ctx.win_stats["n_inbound"] == 0:
            dq.append("No transactions in the investigation window.")
        if self.graph is None:
            dq.append("Graph backend unavailable: relationship signals were not evaluated.")
        return RiskAssessment(
            entity_type=ctx.entity_type, entity_id=ctx.entity_id, customer_id=ctx.customer.customer_id,
            score=score, band=self.config.band(score),
            flagged=score >= self.config.score.investigation_threshold,
            investigation_threshold=self.config.score.investigation_threshold,
            contributors=contributions, signals=signals, not_evaluated=not_eval, metrics=metrics,
            baseline=ctx.base_stats, window=ctx.win_stats, data_quality=dq,
            config_version=self.config.version, config_fingerprint=self.config.fingerprint(),
            computed_at=utcnow(), window_start=ctx.window_start, window_end=ctx.window_end,
            baseline_start=ctx.baseline_start)

    def score(self, signals: list[RiskSignal]) -> tuple[list[Contribution], float]:
        contribs: list[Contribution] = []
        for s in signals:
            sc = self.config.signals.get(s.signal_type)
            w = sc.weight if sc else 0.0
            contribs.append(Contribution(signal_type=s.signal_type, group=sc.group if sc else None, weight=w,
                                         strength=s.strength, raw_points=round(w * s.strength, 2),
                                         points=round(w * s.strength, 2)))
        by_group: dict[str, list[Contribution]] = {}
        for c in contribs:
            if c.group and c.group in self.config.group_caps:
                by_group.setdefault(c.group, []).append(c)
        for g, items in by_group.items():
            remaining = self.config.group_caps[g]
            for c in sorted(items, key=lambda c: -c.raw_points):
                allowed = min(c.raw_points, max(remaining, 0.0))
                if allowed < c.raw_points:
                    c.points, c.capped = round(allowed, 2), True
                remaining -= allowed
        total = min(self.config.score.cap, sum(c.points for c in contribs))
        contribs.sort(key=lambda c: -c.points)
        return contribs, round(total, 1)

    # -------------------------------------------------------------- utilities
    def _signal(self, ctx: _Ctx, name: str, sc: SignalConfig, observed: float | str, baseline: Any,
                threshold: float | str, unit: str, s: float, description: str,
                evidence: list[EvidenceRef], data: dict[str, Any] | None = None,
                confidence: float | None = None) -> RiskSignal:
        conf = confidence if confidence is not None else (0.6 if ctx.thin_baseline else 0.9)
        return RiskSignal(
            signal_id=f"{name}:{ctx.entity_id}:{ctx.window_end:%Y%m%d}", signal_type=name,
            entity_type=ctx.entity_type, entity_id=ctx.entity_id, observed_value=observed,
            baseline_value=baseline, threshold=threshold, unit=unit, severity=severity_of(s), strength=s,
            confidence=conf, description=description, evidence=evidence[:25], timestamp=utcnow(),
            window_start=ctx.window_start, window_end=ctx.window_end, data=data or {})

    def _ramp(self, sc: SignalConfig) -> float:
        return float(sc.p("ramp", self.config.score.ramp))

    def _metric(self, name: str, observed: float, threshold: float, **extra: Any) -> None:
        self._metrics[name] = {"observed": observed, "threshold": threshold, **extra}

    @staticmethod
    def _txn_refs(df: pd.DataFrame, note: str | None = None, limit: int = 10) -> list[EvidenceRef]:
        return [EvidenceRef(kind="transaction", id=str(t), note=note) for t in df.transaction_id.head(limit)]

    # -------------------------------------------------------------- detectors
    def _burst(self, ctx: _Ctx, sc: SignalConfig):
        minutes = int(sc.p("window_minutes", 60))
        obs, at = max_count_in_window(ctx.win.outbound.timestamp, minutes)
        base_max, _ = max_count_in_window(ctx.base.outbound.timestamp, minutes)
        thr = max(float(sc.p("min_count", 6)), base_max * float(sc.p("baseline_multiplier", 2.0)))
        self._metric("TRANSACTION_BURST", obs, thr, baseline=base_max)
        if obs < thr:
            return None
        s = strength(obs, thr, self._ramp(sc))
        o = ctx.win.outbound
        in_burst = o[(o.timestamp >= at) & (o.timestamp <= at + pd.Timedelta(minutes=minutes))]
        failed = int((in_burst.status != "completed").sum())
        desc = (f"{obs} outbound transactions within {minutes} minutes starting {at:%Y-%m-%d %H:%M} UTC "
                f"(threshold {thr:g}; baseline maximum {base_max}); {failed} of them failed.")
        return self._signal(ctx, "TRANSACTION_BURST", sc, obs, base_max, thr, "transactions/hour", s, desc,
                            self._txn_refs(in_burst, "in burst"), {"burst_start": str(at), "failed": failed})

    def _velocity(self, ctx: _Ctx, sc: SignalConfig):
        obs = ctx.win_stats["daily_outbound_max"]
        mean, std = ctx.base_stats["daily_outbound_mean"], ctx.base_stats["daily_outbound_std"]
        thr = max(float(sc.p("min_daily", 6)), mean + float(sc.p("std_multiplier", 4.0)) * std)
        self._metric("VELOCITY_SPIKE", obs, thr, baseline=mean)
        if obs < thr:
            return None
        s = strength(obs, thr, self._ramp(sc))
        o = ctx.win.outbound
        day = o.timestamp.dt.normalize().value_counts().idxmax()
        on_day = o[o.timestamp.dt.normalize() == day]
        desc = (f"{obs} outbound transactions on {day:%Y-%m-%d} versus a baseline daily mean of {mean:.2f} "
                f"(std {std:.2f}); threshold {thr:.1f}.")
        return self._signal(ctx, "VELOCITY_SPIKE", sc, obs, round(mean, 3), round(thr, 2), "transactions/day", s,
                            desc, self._txn_refs(on_day, "peak day"))

    def _amount(self, ctx: _Ctx, sc: SignalConfig):
        b = ctx.base_stats["outbound_usd"]
        done = ctx.win.outbound[ctx.win.outbound.status == "completed"]
        if done.empty:
            self._metric("AMOUNT_DEVIATION", 0.0, 0.0)
            return None
        obs = float(done.amount_usd.max())
        if b.get("n", 0) < int(sc.p("min_baseline_txns", 5)):
            self._metric("AMOUNT_DEVIATION", obs, 0.0, insufficient_baseline=True)
            return NotEvaluated(signal_type="AMOUNT_DEVIATION",
                                reason=f"insufficient baseline ({b.get('n', 0)} outbound transactions)")
        thr = max(float(sc.p("min_usd", 200)), b["median"] + float(sc.p("mad_multiplier", 6)) * b["mad_sigma"],
                  b["p99"] * float(sc.p("p99_multiplier", 2.0)))
        self._metric("AMOUNT_DEVIATION", obs, thr, baseline=b["median"])
        if obs < thr:
            return None
        s = strength(obs, thr, self._ramp(sc))
        above = done[done.amount_usd >= thr].sort_values("amount_usd", ascending=False)
        desc = (f"Largest outbound transaction USD {_fmt(obs)} versus baseline median USD {_fmt(b['median'])} "
                f"and p99 USD {_fmt(b['p99'])}; threshold USD {_fmt(thr)}. {len(above)} transaction(s) above threshold.")
        return self._signal(ctx, "AMOUNT_DEVIATION", sc, round(obs, 2), b["median"], round(thr, 2), "USD", s, desc,
                            self._txn_refs(above, "above threshold"), {"n_above": int(len(above))})

    def _peer_amount(self, ctx: _Ctx, sc: SignalConfig):
        done = ctx.win.outbound[ctx.win.outbound.status == "completed"]
        peer = self.store.peer_amount_stats(ctx.customer.segment, ctx.baseline_start, ctx.window_start)
        if done.empty or "p99_usd" not in peer:
            self._metric("PEER_AMOUNT_DEVIATION", 0.0, 0.0)
            return None
        obs = float(done.amount_usd.max())
        thr = peer["p99_usd"] * float(sc.p("peer_p99_multiplier", 3.0))
        self._metric("PEER_AMOUNT_DEVIATION", obs, thr, baseline=peer["p99_usd"])
        if obs < thr:
            return None
        s = strength(obs, thr, self._ramp(sc))
        above = done[done.amount_usd >= thr].sort_values("amount_usd", ascending=False)
        desc = (f"Largest outbound transaction USD {_fmt(obs)} versus peer segment '{ctx.customer.segment}' "
                f"p99 USD {_fmt(peer['p99_usd'])} (n={int(peer['n'])}); threshold USD {_fmt(thr)}.")
        return self._signal(ctx, "PEER_AMOUNT_DEVIATION", sc, round(obs, 2), round(peer["p99_usd"], 2),
                            round(thr, 2), "USD", s, desc, self._txn_refs(above, "above peer threshold"),
                            {"peer": peer})

    def _pass_through(self, ctx: _Ctx, sc: SignalConfig):
        hours = float(sc.p("horizon_hours", 24))
        types = list(sc.p("inbound_types", ["transfer"]))
        w_in = ctx.win.inbound[ctx.win.inbound.transaction_type.isin(types)]
        b_in = ctx.base.inbound[ctx.base.inbound.transaction_type.isin(types)]
        w = pass_through(w_in, ctx.win.outbound, hours)
        bl = pass_through(b_in, ctx.base.outbound, hours)
        thr = float(sc.p("min_ratio", 0.7))
        self._metric("RAPID_PASS_THROUGH", w["ratio"], thr, baseline=bl["ratio"], inbound_usd=w["inbound_usd"])
        if (w["inbound_usd"] < float(sc.p("min_inbound_usd", 500)) or w["n_inbound"] < int(sc.p("min_inbound_count", 3))
                or w["ratio"] < thr):
            return None
        s = strength(w["ratio"], thr, float(sc.p("ramp", 0.35)))
        refs = []
        for i, o in w["pairs"][:8]:
            refs.append(EvidenceRef(kind="transaction", id=i, note="inbound credit"))
            refs.append(EvidenceRef(kind="transaction", id=o, note=f"outflow within {hours:g}h"))
        desc = (f"{w['ratio']:.0%} of USD {_fmt(w['inbound_usd'])} received across {w['n_inbound']} inbound "
                f"{'/'.join(types)} credits "
                f"left the account(s) within {hours:g} hours (baseline ratio {bl['ratio']:.0%}; threshold {thr:.0%}).")
        return self._signal(ctx, "RAPID_PASS_THROUGH", sc, w["ratio"], bl["ratio"], thr, "share of inbound value", s,
                            desc, refs, {"matched_usd": w["matched_usd"], "inbound_usd": w["inbound_usd"]})

    def _fan_in(self, ctx: _Ctx, sc: SignalConfig):
        cps = inbound_counterparties(ctx.win.inbound)
        obs = int(cps.nunique())
        days = max((ctx.window_end - ctx.window_start).days, 1)
        base_rate = ctx.base_stats["distinct_inbound_per_30d"] * days / 30
        thr = max(float(sc.p("min_counterparties", 8)), base_rate * float(sc.p("baseline_multiplier", 3.0)))
        self._metric("FAN_IN", obs, thr, baseline=base_rate)
        if obs < thr:
            return None
        s = strength(obs, thr, self._ramp(sc))
        internal_senders = ctx.win.inbound.sender_account_id.dropna().unique().tolist()
        refs = self._txn_refs(ctx.win.inbound.sort_values("timestamp"), "inbound credit", 12)
        desc = (f"{obs} distinct counterparties sent funds in the window versus an expected {base_rate:.1f} "
                f"from baseline; threshold {thr:.1f}. {len(internal_senders)} are accounts at this institution.")
        return self._signal(ctx, "FAN_IN", sc, obs, round(base_rate, 2), round(thr, 2), "counterparties", s, desc,
                            refs, {"internal_sender_accounts": internal_senders[:50]})

    def _new_country(self, ctx: _Ctx, sc: SignalConfig):
        base_c = set(ctx.base_stats["countries"]) | {ctx.customer.country}
        win = ctx.win_tx.dropna(subset=["country"])
        new = win[~win.country.isin(base_c)]
        hr = set(self.config.high_risk_jurisdictions)
        obs = int(len(new))
        thr = float(sc.p("min_txns", 1))
        self._metric("GEO_NEW_COUNTRY", obs, thr, n_countries=int(new.country.nunique()))
        if ctx.base_stats["n_outbound"] == 0:
            return NotEvaluated(signal_type="GEO_NEW_COUNTRY", reason="no baseline activity to compare countries")
        if obs < thr:
            return None
        s = strength(obs, thr, self._ramp(sc) * 2)
        countries = sorted(new.country.unique().tolist())
        hr_hit = sorted(set(countries) & hr)
        if hr_hit:
            s = max(s, 0.9)
        desc = (f"{obs} transaction(s) in {len(countries)} country/countries not seen in the baseline or KYC home "
                f"country {ctx.customer.country}: {', '.join(countries)}."
                + (f" Configured high-risk jurisdiction(s): {', '.join(hr_hit)}." if hr_hit else ""))
        return self._signal(ctx, "GEO_NEW_COUNTRY", sc, obs, ", ".join(sorted(base_c)), thr, "transactions", s, desc,
                            self._txn_refs(new, "new country"), {"new_countries": countries, "high_risk": hr_hit})

    def _impossible_travel(self, ctx: _Ctx, sc: SignalConfig):
        v = detect_impossible_travel(ctx.win.outbound, float(sc.p("max_speed_kmh", 900)),
                                     float(sc.p("min_distance_km", 500)), bool(sc.p("physical_channels_only", True)))
        thr = float(sc.p("max_speed_kmh", 900))
        obs = max((x.speed_kmh for x in v), default=0.0)
        self._metric("IMPOSSIBLE_TRAVEL", obs, thr, violations=len(v))
        if not v:
            return None
        s = strength(obs, thr, self._ramp(sc))
        worst = max(v, key=lambda x: x.speed_kmh)
        refs = []
        for x in v[:5]:
            refs += [EvidenceRef(kind="transaction", id=x.from_txn, note="location A"),
                     EvidenceRef(kind="transaction", id=x.to_txn,
                                 note=f"{x.distance_km:,.0f} km in {x.hours:.2f} h")]
        desc = (f"{len(v)} pair(s) of card-present transactions imply travel faster than {thr:g} km/h; worst case "
                f"{worst.distance_km:,.0f} km in {worst.hours:.2f} h (~{worst.speed_kmh:,.0f} km/h).")
        return self._signal(ctx, "IMPOSSIBLE_TRAVEL", sc, obs, None, thr, "km/h", s, desc, refs,
                            {"violations": [x.__dict__ for x in v[:10]]})

    def _new_device(self, ctx: _Ctx, sc: SignalConfig):
        base_devs = set(ctx.base_stats["devices"])
        o = ctx.win.outbound.dropna(subset=["device_id"])
        if o.empty:
            self._metric("NEW_DEVICE", 0.0, float(sc.p("min_value_share", 0.3)))
            return None
        new = o[~o.device_id.isin(base_devs)]
        done = o[o.status == "completed"]
        share = float(new[new.status == "completed"].amount_usd.sum() / done.amount_usd.sum()) if done.amount_usd.sum() else 0.0
        thr = float(sc.p("min_value_share", 0.3))
        self._metric("NEW_DEVICE", share, thr, n_new_device_txns=int(len(new)))
        if new.empty or (share < thr and len(new) < int(sc.p("min_txns", 3))):
            return None
        s = strength(max(share, thr), thr, float(sc.p("ramp", 1.0)))
        devs = sorted(new.device_id.unique().tolist())
        refs = [EvidenceRef(kind="device", id=d, note="first used in window") for d in devs[:5]]
        refs += self._txn_refs(new, "via new device", 8)
        conf = 0.5 if not base_devs else (0.6 if ctx.thin_baseline else 0.85)
        desc = (f"{len(devs)} device(s) never used in the baseline initiated {len(new)} outbound transaction(s), "
                f"{share:.0%} of completed outbound digital value in the window (threshold {thr:.0%}).")
        return self._signal(ctx, "NEW_DEVICE", sc, round(share, 4), len(base_devs), thr, "share of value", s, desc,
                            refs, {"new_devices": devs}, confidence=conf)

    def _device_sharing(self, ctx: _Ctx, sc: SignalConfig):
        lookback = int(sc.p("lookback_days", 90))
        start = ctx.window_end - timedelta(days=lookback)
        mine = ctx.win.outbound.device_id.dropna().unique().tolist()
        if not mine:
            self._metric("DEVICE_SHARING", 0.0, float(sc.p("min_customers", 3)))
            return None
        users = self.store.device_users(mine, start, ctx.window_end)
        per = users.groupby("device_id").customer_id.nunique() if not users.empty else pd.Series(dtype=int)
        obs = int(per.max()) if len(per) else 0
        thr = float(sc.p("min_customers", 3))
        self._metric("DEVICE_SHARING", obs, thr)
        if obs < thr:
            return None
        s = strength(obs, thr, self._ramp(sc))
        shared = per[per >= thr].sort_values(ascending=False)
        refs = []
        details = []
        for d, n in shared.items():
            others = sorted(set(users[users.device_id == d].customer_id) - {ctx.customer.customer_id})
            refs.append(EvidenceRef(kind="device", id=str(d), note=f"used by {n} customers"))
            refs += [EvidenceRef(kind="customer", id=c, note=f"also uses {d}") for c in others[:8]]
            details.append({"device_id": d, "n_customers": int(n), "other_customers": others[:20]})
        desc = (f"Device {shared.index[0]} was used by {obs} distinct customers in the last {lookback} days "
                f"(threshold {thr:g}).")
        return self._signal(ctx, "DEVICE_SHARING", sc, obs, None, thr, "customers per device", s, desc, refs,
                            {"shared_devices": details})

    def _shared_identifier(self, ctx: _Ctx, sc: SignalConfig):
        rows = self.store.customers_sharing_identifiers(ctx.customer.customer_id)
        always = set(sc.p("always_flag_types", ["phone", "email"]))
        thr = float(sc.p("min_customers", 3))
        by_type: dict[tuple[str, str], set[str]] = {}
        for r in rows:
            by_type.setdefault((r["identifier_type"], r["masked_value"]), set()).add(r["customer_id"])
        hits = {k: v for k, v in by_type.items() if len(v) + 1 >= thr or (k[0] in always and v)}
        obs = max((len(v) + 1 for v in by_type.values()), default=1)
        self._metric("SHARED_IDENTIFIER", obs, thr)
        if not hits:
            return None
        s = strength(max(obs, thr), thr, self._ramp(sc))
        refs = [EvidenceRef(kind="customer", id=c, note=f"shares {t} {m}") for (t, m), cs in hits.items() for c in sorted(cs)]
        desc = "; ".join(f"{t} {m} shared with {len(cs)} other customer(s)" for (t, m), cs in hits.items()) + "."
        return self._signal(ctx, "SHARED_IDENTIFIER", sc, obs, None, thr, "customers per identifier", s, desc, refs)

    def _circular(self, ctx: _Ctx, sc: SignalConfig):
        if self.graph is None:
            return NotEvaluated(signal_type="CIRCULAR_FLOW", reason="graph backend unavailable")
        max_len = int(sc.p("max_cycle_length", 5))
        cands = self.graph.candidate_cycles(sorted(ctx.account_ids), max_len, since=ctx.window_start)
        gap = pd.Timedelta(hours=float(sc.p("max_hop_gap_hours", 72)))
        tol = float(sc.p("amount_tolerance", 0.3))
        verified = []
        for cyc in cands[:50]:
            tx = self.store.transactions_for_accounts(cyc, ctx.window_start, ctx.window_end)
            tx = tx[(tx.status == "completed") & tx.sender_account_id.isin(cyc) & tx.receiver_account_id.isin(cyc)]
            chain = _verify_temporal_cycle(cyc, tx, gap, tol)
            if chain is not None:
                verified.append((cyc, chain))
        thr = float(sc.p("min_cycle_usd", 500))
        obs = max((float(ch["amount_usd"].iloc[0]) for _, ch in verified), default=0.0)
        self._metric("CIRCULAR_FLOW", obs, thr, candidates=len(cands), verified=len(verified))
        if not verified or obs < thr:
            return None
        s = strength(obs, thr, self._ramp(sc))
        refs = []
        details = []
        for cyc, ch in verified[:5]:
            refs.append(EvidenceRef(kind="cycle", id=" -> ".join(cyc + [cyc[0]]), note=f"{len(cyc)} hops"))
            refs += self._txn_refs(ch, "cycle hop", 6)
            details.append({"accounts": cyc, "transactions": ch.transaction_id.tolist(),
                            "start_usd": round(float(ch.amount_usd.iloc[0]), 2),
                            "end_usd": round(float(ch.amount_usd.iloc[-1]), 2),
                            "duration_hours": round((ch.timestamp.iloc[-1] - ch.timestamp.iloc[0]).total_seconds() / 3600, 1)})
        d0 = details[0]
        desc = (f"{len(verified)} temporally ordered circular flow(s) return funds to the subject's account; e.g. "
                f"{' -> '.join(d0['accounts'] + [d0['accounts'][0]])} moved USD {_fmt(d0['start_usd'])} and returned "
                f"USD {_fmt(d0['end_usd'])} within {d0['duration_hours']} hours.")
        return self._signal(ctx, "CIRCULAR_FLOW", sc, round(obs, 2), None, thr, "USD", s, desc, refs,
                            {"cycles": details}, confidence=0.9)

    def _dormant(self, ctx: _Ctx, sc: SignalConfig):
        min_days = int(sc.p("min_dormant_days", 90))
        min_usd = float(sc.p("min_window_usd", 1000))
        hits = []
        best = 0.0
        for a in ctx.accounts:
            win = ctx.win_tx[(ctx.win_tx.sender_account_id == a.account_id) | (ctx.win_tx.receiver_account_id == a.account_id)]
            if win.empty:
                continue
            base = ctx.base_tx[(ctx.base_tx.sender_account_id == a.account_id) | (ctx.base_tx.receiver_account_id == a.account_id)]
            if not base.empty:
                continue
            opened = pd.Timestamp(a.opened_at)
            if opened.tzinfo is None:
                opened = opened.tz_localize("UTC")
            if opened > pd.Timestamp(ctx.baseline_start):
                continue  # new account, not dormant
            older = self.store.transactions_for_accounts([a.account_id], None, ctx.baseline_start)
            last = older.timestamp.max() if not older.empty else opened
            first_win = win.timestamp.min()
            gap = (first_win - last).total_seconds() / 86400
            vol = float(win[win.status == "completed"].amount_usd.sum())
            best = max(best, gap)
            if gap >= min_days and vol >= min_usd:
                hits.append((a.account_id, round(gap, 1), round(vol, 2), win, last))
        self._metric("DORMANT_REACTIVATION", best, float(min_days))
        if not hits:
            return None
        acc, gap, vol, win, last = max(hits, key=lambda h: h[2])
        s = strength(gap, min_days, self._ramp(sc))
        refs = [EvidenceRef(kind="account", id=acc, note=f"inactive {gap:.0f} days")] + self._txn_refs(win, "reactivation")
        desc = (f"Account {acc} had no activity for {gap:.0f} days (last activity {pd.Timestamp(last):%Y-%m-%d}) and "
                f"then moved USD {_fmt(vol)} in the window (thresholds: {min_days} days, USD {_fmt(min_usd)}).")
        return self._signal(ctx, "DORMANT_REACTIVATION", sc, gap, None, float(min_days), "days inactive", s, desc,
                            refs, {"account_id": acc, "window_usd": vol})

    def _high_risk_merchant(self, ctx: _Ctx, sc: SignalConfig):
        def hr_frame(df: pd.DataFrame) -> pd.DataFrame:
            d = df.dropna(subset=["merchant_id"])
            d = d[d.status == "completed"]
            if d.empty:
                return d
            risk = {m.merchant_id: m.risk_profile for m in self.store.get_merchants(d.merchant_id.unique().tolist())}
            return d[d.merchant_id.map(risk) == "high"]

        win = hr_frame(ctx.win.outbound)
        base = hr_frame(ctx.base.outbound)
        days_w = max((ctx.window_end - ctx.window_start).days, 1)
        days_b = max((ctx.window_start - ctx.baseline_start).days, 1)
        base_rate = float(base.amount_usd.sum()) * days_w / days_b
        thr = max(float(sc.p("min_usd", 300)), base_rate * float(sc.p("baseline_multiplier", 3.0)))
        obs = float(win.amount_usd.sum()) if not win.empty else 0.0
        self._metric("HIGH_RISK_MERCHANT", obs, thr, n=int(len(win)))
        if len(win) < int(sc.p("min_txns", 3)) or obs < thr:
            return None
        s = strength(obs, thr, self._ramp(sc))
        merchants = win.merchant_id.value_counts()
        refs = [EvidenceRef(kind="merchant", id=m, note=f"{n} payments") for m, n in merchants.head(5).items()]
        refs += self._txn_refs(win.sort_values("timestamp"), "high-risk merchant")
        desc = (f"{len(win)} payments totalling USD {_fmt(obs)} to {len(merchants)} merchant(s) rated high-risk, "
                f"versus an expected USD {_fmt(base_rate)} from baseline; threshold USD {_fmt(thr)}.")
        return self._signal(ctx, "HIGH_RISK_MERCHANT", sc, round(obs, 2), round(base_rate, 2), round(thr, 2), "USD",
                            s, desc, refs, {"merchants": merchants.head(10).to_dict()})

    def _behaviour_shift(self, ctx: _Ctx, sc: SignalConfig):
        p, q = ctx.base_stats["type_channel_mix"], ctx.win_stats["type_channel_mix"]
        n = ctx.win_stats["n_outbound"]
        thr = float(sc.p("min_jsd", 0.35))
        if not p or n < int(sc.p("min_window_txns", 8)):
            self._metric("BEHAVIOURAL_SHIFT", 0.0, thr)
            return None
        js = jensen_shannon(p, q)
        self._metric("BEHAVIOURAL_SHIFT", js, thr)
        if js < thr:
            return None
        s = strength(js, thr, self._ramp(sc))
        top_w = sorted(q.items(), key=lambda kv: -kv[1])[:3]
        top_b = sorted(p.items(), key=lambda kv: -kv[1])[:3]
        desc = (f"Jensen-Shannon divergence {js:.2f} between baseline and window transaction type/channel mix "
                f"(threshold {thr:.2f}). Window: {', '.join(f'{k} {v:.0%}' for k, v in top_w)}; baseline: "
                f"{', '.join(f'{k} {v:.0%}' for k, v in top_b)}.")
        return self._signal(ctx, "BEHAVIOURAL_SHIFT", sc, round(js, 4), None, thr, "JSD", s, desc, [],
                            {"window_mix": dict(top_w), "baseline_mix": dict(top_b)})

    def _network_exposure(self, ctx: _Ctx, sc: SignalConfig):
        if self.graph is None:
            return NotEvaluated(signal_type="NETWORK_EXPOSURE", reason="graph backend unavailable")
        cps = self.graph.counterparties(sorted(ctx.account_ids), since=ctx.window_start)
        owners = {a: self.graph.account_owner(a) for a in cps}
        owner_ids = sorted({o for o in owners.values() if o and o != ctx.customer.customer_id})
        flags = self.graph.customer_flags(owner_ids)
        confirmed = {i.subject_id for i in self.store.list_investigations(subject_ids=owner_ids, limit=500)
                     if i.conclusion == "confirmed_suspicious"}
        flagged = sorted({c for c, f in flags.items() if f} | confirmed)
        thr = float(sc.p("min_flagged", 1))
        self._metric("NETWORK_EXPOSURE", len(flagged), thr, counterparties=len(owner_ids))
        if len(flagged) < thr:
            return None
        s = strength(len(flagged), thr, self._ramp(sc) * 2)
        refs = [EvidenceRef(kind="customer", id=c, note="counterparty with open alert or confirmed case") for c in flagged[:10]]
        desc = (f"{len(flagged)} of {len(owner_ids)} direct transfer counterparties in the window have an open alert "
                f"or a previously confirmed suspicious case.")
        return self._signal(ctx, "NETWORK_EXPOSURE", sc, len(flagged), len(owner_ids), thr, "flagged counterparties",
                            s, desc, refs, {"flagged": flagged[:50]})

    def _historical_alerts(self, ctx: _Ctx, sc: SignalConfig):
        alerts = self.store.list_alerts(entity_id=ctx.customer.customer_id, before=ctx.window_start, limit=50)
        n = len(alerts)
        self._metric("HISTORICAL_ALERTS", n, 1)
        if n == 0:
            return None
        s = round(min(1.0, n / float(sc.p("saturate_at", 3))), 4)
        refs = [EvidenceRef(kind="alert", id=a.alert_id, note=f"{a.alert_type} ({a.status})") for a in alerts[:10]]
        statuses = sorted({a.status for a in alerts})
        desc = f"{n} alert(s) raised before the window ({', '.join(statuses)})."
        return self._signal(ctx, "HISTORICAL_ALERTS", sc, n, None, 1, "alerts", s, desc, refs, confidence=0.7)

    def _ml(self, ctx: _Ctx, sc: SignalConfig, metrics: dict[str, dict[str, Any]]):
        pct, raw = self.ml_model.percentile(metrics, ctx.base_stats, ctx.win_stats)
        thr = float(sc.p("min_percentile", 0.99))
        metrics["ML_ANOMALY"] = {"observed": pct, "threshold": thr, "raw_score": raw}
        if pct < thr:
            return None
        s = round(0.5 + 0.5 * min(1.0, (pct - thr) / max(1 - thr, 1e-6)), 4)
        top = self.ml_model.top_features(metrics, ctx.base_stats, ctx.win_stats)
        desc = (f"Isolation-forest anomaly score is at the {pct:.1%} percentile of the training population "
                f"(threshold {thr:.0%}). Most unusual features: {', '.join(f'{k}={v:.2f}' for k, v in top)}.")
        return self._signal(ctx, "ML_ANOMALY", sc, round(pct, 4), None, thr, "percentile", s, desc, [],
                            {"model": self.ml_model.describe(), "top_features": top}, confidence=0.6)


def _verify_temporal_cycle(cycle: list[str], tx: pd.DataFrame, gap: pd.Timedelta, tol: float) -> pd.DataFrame | None:
    """Find a chain of transfers following the cycle in time order with similar amounts.

    Tries each rotation of the cycle as the starting point. Returns the chain's
    transactions (one per hop) or None.
    """
    if tx.empty:
        return None
    k = len(cycle)
    hops = {}
    for i in range(k):
        a, b = cycle[i], cycle[(i + 1) % k]
        hops[(a, b)] = tx[(tx.sender_account_id == a) & (tx.receiver_account_id == b)].sort_values("timestamp")
    for r in range(k):
        rot = cycle[r:] + cycle[:r]
        first = hops[(rot[0], rot[1])]
        for _, t0 in first.iterrows():
            chain = [t0]
            ok = True
            for i in range(1, k):
                a, b = rot[i], rot[(i + 1) % k]
                prev = chain[-1]
                cand = hops[(a, b)]
                cand = cand[(cand.timestamp > prev.timestamp) & (cand.timestamp <= prev.timestamp + gap)]
                cand = cand[(cand.amount_usd / prev.amount_usd - 1).abs() <= tol]
                if cand.empty:
                    ok = False
                    break
                chain.append(cand.iloc[0])
            if ok:
                return pd.DataFrame(chain)
    return None


def safe_float(x: Any) -> float:
    try:
        f = float(x)
        return f if math.isfinite(f) else 0.0
    except (TypeError, ValueError):
        return 0.0


__all__ = ["RiskEngine", "EntityNotFound", "strength", "severity_of", "np"]
