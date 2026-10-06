"""Monitoring service: monitoring runs, alert generation and deduplication, alert and case workflow.

Everything here is deterministic. No LLM takes part in deciding whether an alert exists, how it is
scored or how it moves through its lifecycle.

Errors raised (mapped to HTTP status codes by the API layer):
  NotFoundError -> 404, AccessError -> 403, ConflictError -> 409 (illegal transition, duplicate),
  ValueError    -> 422 (bad input).
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pandas as pd

from app.core.observability import METRICS, log_event, request_id_var
from app.data.store import new_id, utcnow
from app.monitoring import lifecycle as lc
from app.monitoring import mule as ml
from app.monitoring import quality as ql
from app.monitoring import triage as tg
from app.monitoring.detectors import detector_results
from app.monitoring.models import (
    AlertEvent,
    Case,
    CaseEvent,
    CaseEvidence,
    CaseNote,
    DetectionResult,
    IngestionBatch,
    MonitoringAlert,
    MonitoringRun,
    RejectedRow,
)
from app.monitoring.repository import (
    AlertFilter,
    CaseFilter,
    DuplicateOpenCase,
    DuplicateUnresolvedAlert,
)
from app.risk.catalog import CATALOG, category_of
from app.risk.engine import EntityNotFound
from app.schemas.domain import AuditEvent, HumanDecision
from app.security.principal import Principal

log = logging.getLogger("fira.monitoring")

BAND_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
SEV_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
MAX_CUSTOMERS_PER_RUN = 5000
# case decision -> existing investigation decision (kept consistent, see docs)
INVESTIGATION_DECISION = {"CONFIRMED_SUSPICIOUS": "confirm", "CLEARED": "reject", "FALSE_POSITIVE": "reject",
                          "ESCALATED": "escalate"}
INVESTIGATION_EFFECT = {"confirm": ("closed", "confirmed_suspicious"), "reject": ("closed", "legitimate"),
                        "escalate": ("pending_review", "escalated")}


class NotFoundError(LookupError):
    pass


class AccessError(PermissionError):
    pass


class ConflictError(ValueError):
    pass


def severity_for(signal_severity: str, band: str, critical_band: str = "critical") -> str:
    """Detector severity, raised to critical when the customer's overall band is critical (or above)."""
    if BAND_RANK.get(band, 0) >= BAND_RANK.get(critical_band, 3):
        return "critical"
    return signal_severity if signal_severity in SEV_RANK else "medium"


class MonitoringService:
    def __init__(self, container: Any):
        self.c = container
        self._tl = threading.local()

    # ------------------------------------------------------------------ shared helpers
    @property
    def repo(self) -> Any:
        return self.c.monitoring_repo

    @property
    def cfg(self) -> Any:
        return self.c.monitoring_config

    @property
    def store(self) -> Any:
        return self.c.store

    @contextmanager
    def _unit(self) -> Iterator[None]:
        """One workflow operation = one repository transaction.

        Audit entries written while the unit is open are held back and written only after the transaction
        commits, so the audit trail never records an action that was rolled back. (PostgreSQL repository:
        real rollback. In-memory repository: serialised, no rollback.)
        """
        depth = getattr(self._tl, "depth", 0)
        if depth == 0:
            self._tl.pending = []
        self._tl.depth = depth + 1
        try:
            with self.repo.transaction():
                yield
        except BaseException:
            self._tl.depth = depth
            if depth == 0:
                self._tl.pending = []
            raise
        self._tl.depth = depth
        if depth == 0:
            pending, self._tl.pending = self._tl.pending, []
            for ev in pending:
                self.store.append_audit(ev)

    def _audit(self, actor: Principal | None, action: str, entity_type: str | None, entity_id: str | None,
               result: str = "ok", **details: Any) -> None:
        ev = AuditEvent(ts=utcnow(), user_id=actor.user_id if actor else "system",
                        role=actor.role if actor else "system", action=action, entity_type=entity_type,
                        entity_id=entity_id, result=result, request_id=request_id_var.get(), details=details)
        if getattr(self._tl, "depth", 0) > 0:
            self._tl.pending.append(ev)
        else:
            self.store.append_audit(ev)

    @staticmethod
    def _can_act(actor: Principal, assigned_to: str | None) -> bool:
        return actor.has("admin") or assigned_to is None or assigned_to == actor.user_id

    def _require_user(self, user_id: str) -> None:
        if not user_id.startswith("U-") or self.store.get_user(user_id[2:]) is None:
            raise ValueError(f"unknown user {user_id}")

    def _alert_or_404(self, alert_id: str) -> MonitoringAlert:
        a = self.repo.get_alert(alert_id)
        if a is None:
            raise NotFoundError(f"alert {alert_id} not found")
        return a

    def _case_or_404(self, case_id: str) -> Case:
        cs = self.repo.get_case(case_id)
        if cs is None:
            raise NotFoundError(f"case {case_id} not found")
        return cs

    def _alert_event(self, alert_id: str, actor: Principal | None, event_type: str, frm: str | None = None,
                     to: str | None = None, **detail: Any) -> None:
        self.repo.add_alert_event(AlertEvent(alert_id=alert_id, ts=utcnow(),
                                             actor=actor.user_id if actor else "system", event_type=event_type,
                                             from_status=frm, to_status=to, detail=detail))

    def _case_event(self, case_id: str, actor: Principal | None, event_type: str, frm: str | None = None,
                    to: str | None = None, **detail: Any) -> None:
        self.repo.add_case_event(CaseEvent(case_id=case_id, ts=utcnow(), actor=actor.user_id if actor else "system",
                                           event_type=event_type, from_status=frm, to_status=to, detail=detail))

    # ------------------------------------------------------------------ monitoring run
    def run(self, *, window_end: Any = None, lookback_days: int | None = None, customer_ids: list[str] | None = None,
            active_days: int | None = None, actor: Principal | None = None, mode: str = "batch",
            max_customers: int = MAX_CUSTOMERS_PER_RUN) -> MonitoringRun:
        """Screen customers, raise/merge alerts, and record the run."""
        t0 = time.perf_counter()
        started = utcnow()
        cfg = self.cfg
        end = pd.Timestamp(window_end).to_pydatetime() if window_end is not None else self.store.as_of()
        lookback = lookback_days or cfg.lookback_days
        window_start = end - timedelta(days=lookback)
        act_days = active_days or cfg.active_days
        activity_start = end - timedelta(days=act_days)
        in_scope = self.store.count_transactions(activity_start, end)
        ids = sorted(set(customer_ids)) if customer_ids is not None else self.store.active_customers(activity_start, end)
        truncated = len(ids) > max_customers
        ids = ids[:max_customers]
        run = MonitoringRun(
            run_id=new_id("RUN"), started_at=started, mode=mode, window_start=window_start, window_end=end,
            lookback_days=lookback, config_version=self.c.risk_config.version,
            config_fingerprint=self.c.risk_config.fingerprint(), transactions_in_scope=in_scope,
            triggered_by=actor.user_id if actor else "system",
            details={"monitoring_config": cfg.version, "monitoring_fingerprint": cfg.fingerprint(),
                     "active_days": act_days, "truncated": truncated})
        # the run row exists before alerts reference it
        self.repo.save_run(run)
        created = updated = suppressed = errors = 0
        t_assess = t_alert = 0.0
        standalone, supporting = cfg.standalone_detectors(), cfg.supporting_detectors()
        gate = (cfg.alerting.supporting_min_customer_score if cfg.alerting.supporting_min_customer_score is not None
                else self.c.risk_config.score.investigation_threshold)
        for cid in ids:
            t_a = time.perf_counter()
            try:
                a = self.c.risk_engine.assess_customer(cid, end, lookback, cfg.baseline_days)
                t_assess += time.perf_counter() - t_a
            except EntityNotFound:
                errors += 1
                continue
            except Exception as exc:  # one bad customer must not stop the run
                errors += 1
                log_event(log, "assessment_failed", logging.WARNING, customer_id=cid, exc_type=type(exc).__name__)
                continue
            points = {c.signal_type: c.points for c in a.contributors}
            cands = [r for r in detector_results(a, include_not_triggered=False)
                     if points.get(r.detector_id, 0.0) >= cfg.alerting.min_detector_points
                     and (r.detector_id in standalone or (r.detector_id in supporting and a.score >= gate))]
            if not cands:
                continue
            t_b = time.perf_counter()
            accounts = {x.account_id for x in self.store.accounts_for_customer(cid)}
            txf = self.store.transactions_for_accounts(sorted(accounts), window_start, end)
            for res in cands:
                outcome = self._upsert_alert(run, a, res, accounts, txf)
                created += outcome == "created"
                updated += outcome == "updated"
                suppressed += outcome == "suppressed"
            t_alert += time.perf_counter() - t_b
        run = run.model_copy(update={
            "finished_at": utcnow(), "customers_evaluated": len(ids), "alerts_created": created,
            "alerts_updated": updated, "alerts_suppressed": suppressed, "errors": errors,
            "duration_ms": int((time.perf_counter() - t0) * 1000),
            "details": {**run.details, "timing_ms": {"risk_assessment": int(t_assess * 1000),
                                                      "alert_generation": int(t_alert * 1000)}}})
        self.repo.save_run(run)
        self._audit(actor, "monitoring_run", "monitoring_run", run.run_id, customers=len(ids), created=created,
                    updated=updated, suppressed=suppressed, errors=errors)
        log_event(log, "monitoring_run", run_id=run.run_id, customers=len(ids), created=created, updated=updated,
                  suppressed=suppressed, errors=errors, duration_ms=run.duration_ms)
        return run

    def _upsert_alert(self, run: MonitoringRun, a: Any, res: DetectionResult, accounts: set[str],
                      txf: pd.DataFrame) -> str:
        cid = res.customer_id
        severity = severity_for(res.severity or "medium", a.band, self.cfg.alerting.critical_band)
        txn_ids = list(res.transaction_ids)
        existing = self.repo.find_unresolved(cid, res.detector_id)
        now = utcnow()
        if existing is not None:
            return self._merge(existing, a, res, severity, txn_ids, now, txf)
        # suppression of already-adjudicated behaviour
        if txn_ids:
            seen = self.repo.resolved_alert_transactions(cid, res.detector_id)
            if seen and set(txn_ids) <= seen:
                return "suppressed"
        else:
            last = self.repo.last_resolved_at(cid, res.detector_id)
            if last is not None and now - last < timedelta(days=self.cfg.deduplication.resolved_cooldown_days):
                return "suppressed"
        account_id, first_txn = self._primary(txn_ids, accounts, txf, res)
        info = CATALOG.get(res.detector_id)
        alert = MonitoringAlert(
            alert_id=new_id("MAL"), run_id=run.run_id, customer_id=cid, account_id=account_id,
            transaction_id=first_txn, detector_id=res.detector_id, alert_type=res.detector_id,
            category=info.category if info else "other", severity=severity,  # type: ignore[arg-type]
            risk_score=a.score, risk_contribution=res.risk_contribution, status="NEW", description=res.reason,
            explanation=res.explain(), occurrence_count=1, window_start=res.window_start, window_end=res.window_end,
            triggered_at=now, created_at=now, updated_at=now, last_seen_at=now, transaction_ids=txn_ids)
        ctx = self._triage_context(a, txn_ids, txf)
        alert.explanation["triage_context"] = ctx
        if self.cfg.triage.enabled:
            tr = tg.compute_triage(self._triage_inputs(alert, ctx, now), self.cfg.triage)
            alert = alert.model_copy(update={"triage_score": tr.score, "triage_priority": tr.priority,
                                             "triage_factors": tr.factors, "triage_computed_at": now})
        try:
            with self._unit():
                self.repo.create_alert(alert)
                self._alert_event(alert.alert_id, None, "created", None, "NEW", run_id=run.run_id,
                                  detector_id=res.detector_id, risk_score=a.score,
                                  triage_score=alert.triage_score, triage_priority=alert.triage_priority)
                self._audit(None, "alert_created", "monitoring_alert", alert.alert_id, customer_id=cid,
                            detector_id=res.detector_id, severity=severity, risk_score=a.score, run_id=run.run_id)
                self._audit(None, "risk_score_generated", "customer", cid, score=a.score, band=a.band,
                            config_version=a.config_version, alert_id=alert.alert_id)
        except DuplicateUnresolvedAlert:  # lost a race with a concurrent run: merge instead
            again = self.repo.find_unresolved(cid, res.detector_id)
            return self._merge(again, a, res, severity, txn_ids, now, txf) if again else "suppressed"
        return "created"

    def _merge(self, existing: MonitoringAlert, a: Any, res: DetectionResult, severity: str,
               txn_ids: list[str], now: Any, txf: pd.DataFrame | None = None) -> str:
        fields: dict[str, Any] = {
            "occurrence_count": existing.occurrence_count + 1, "last_seen_at": now, "updated_at": now,
            "window_end": res.window_end or existing.window_end,
            "risk_score": max(existing.risk_score, a.score),
            "risk_contribution": max(existing.risk_contribution, res.risk_contribution),
        }
        if SEV_RANK[severity] > SEV_RANK[existing.severity]:
            fields["severity"] = severity
        if res.risk_contribution >= existing.risk_contribution:
            fields["description"], fields["explanation"] = res.reason, res.explain()
        new_txns = [t for t in txn_ids if t not in set(existing.transaction_ids)]
        merged = existing.model_copy(update=fields)
        ctx = dict(existing.explanation.get("triage_context") or {})
        ctx.update(self._triage_context(a, [], None))
        extra = self._amount_of(new_txns, txf)
        prior_amount = ctx.get("amount_usd")
        ctx["amount_usd"] = round((prior_amount or 0.0) + extra, 2) if (extra or prior_amount is not None) else None
        ctx["txn_count"] = int(ctx.get("txn_count", len(existing.transaction_ids))) + len(new_txns)
        merged.explanation = {**merged.explanation, "triage_context": ctx}
        fields["explanation"] = merged.explanation
        prev_priority = existing.triage_priority
        if self.cfg.triage.enabled:
            tr = tg.compute_triage(self._triage_inputs(merged, ctx, now), self.cfg.triage)
            fields.update(triage_score=tr.score, triage_priority=tr.priority, triage_factors=tr.factors,
                          triage_computed_at=now)
        with self._unit():
            self.repo.update_alert(existing.alert_id, **fields)
            if fields.get("triage_priority") and fields["triage_priority"] != prev_priority:
                self._alert_event(existing.alert_id, None, "priority_changed", existing.status, existing.status,
                                  previous=prev_priority, current=fields["triage_priority"],
                                  triage_score=fields["triage_score"])
                self._audit(None, "alert_priority_changed", "monitoring_alert", existing.alert_id,
                            previous=prev_priority, current=fields["triage_priority"], triage_score=fields["triage_score"])
            if new_txns:
                self.repo.add_alert_transactions(existing.alert_id, new_txns)
            self._alert_event(existing.alert_id, None, "updated", existing.status, existing.status,
                              occurrence_count=fields["occurrence_count"], new_transactions=len(new_txns))
            self._audit(None, "alert_updated", "monitoring_alert", existing.alert_id,
                        occurrence_count=fields["occurrence_count"], new_transactions=len(new_txns))
        return "updated"

    @staticmethod
    def _primary(txn_ids: list[str], accounts: set[str], txf: pd.DataFrame, res: DetectionResult) -> tuple[str | None, str | None]:
        """Earliest triggering transaction and the customer's account it involves."""
        if txn_ids and not txf.empty:
            rows = txf[txf.transaction_id.isin(txn_ids)].sort_values("timestamp")
            if not rows.empty:
                r = rows.iloc[0]
                acc = r.sender_account_id if r.sender_account_id in accounts else r.receiver_account_id
                return (str(acc) if isinstance(acc, str) else None), str(r.transaction_id)
        return (res.account_ids[0] if res.account_ids else None), (txn_ids[0] if txn_ids else None)

    # ------------------------------------------------------------------ triage
    @staticmethod
    def _amount_of(txn_ids: list[str], txf: pd.DataFrame | None) -> float:
        if not txn_ids or txf is None or txf.empty:
            return 0.0
        return float(txf[txf.transaction_id.isin(txn_ids)].amount_usd.fillna(0).sum())

    def _triage_context(self, a: Any, txn_ids: list[str], txf: pd.DataFrame | None) -> dict[str, Any]:
        """Customer-level facts known when the alert is raised; stored with the alert so triage can be recomputed."""
        live = [c for c in a.contributors if c.points > 0]
        return {"categories": len({category_of(c.signal_type) for c in live}),
                "network_points": round(sum(c.points for c in live if c.signal_type == "NETWORK_EXPOSURE"), 2),
                "identity_signals": sum(1 for c in live if category_of(c.signal_type) == "device_identity"),
                "amount_usd": round(self._amount_of(txn_ids, txf), 2) if txn_ids else None,
                "txn_count": len(txn_ids)}

    def _history(self, alert: MonitoringAlert) -> tuple[int, int]:
        """(earlier confirmed alerts for the customer, earlier clearances of the same detector). Resolved alerts
        only: the alert's own outcome is never an input."""
        items, _ = self.repo.list_alerts(AlertFilter(customer_id=alert.customer_id, statuses=["RESOLVED"], limit=200))
        others = [x for x in items if x.alert_id != alert.alert_id]
        confirmed = sum(1 for x in others if x.resolution == "CONFIRMED_SUSPICIOUS")
        cleared = sum(1 for x in others if x.detector_id == alert.detector_id
                      and x.resolution in ("CLEARED", "FALSE_POSITIVE"))
        return confirmed, cleared

    def _triage_inputs(self, alert: MonitoringAlert, ctx: dict[str, Any], now: Any) -> tg.TriageInputs:
        confirmed, cleared = self._history(alert)
        age = None if alert.status == "RESOLVED" else max((now - alert.triggered_at).total_seconds() / 3600.0, 0.0)
        return tg.TriageInputs(
            severity=alert.severity, customer_score=alert.risk_score, tier=self.cfg.alert_tier(alert.detector_id),
            categories=int(ctx.get("categories", 1)), amount_usd=ctx.get("amount_usd"),
            txn_count=int(ctx.get("txn_count", len(alert.transaction_ids))), occurrence_count=alert.occurrence_count,
            network_points=float(ctx.get("network_points", 0.0)), identity_signals=int(ctx.get("identity_signals", 0)),
            prior_confirmed=confirmed, prior_cleared_same_detector=cleared, age_hours=age)

    def recompute_triage(self, actor: Principal | None, alert_id: str | None = None) -> dict[str, Any]:
        """Re-score unresolved alerts (their age and the customer's outcome history change over time)."""
        if alert_id:
            targets = [self._alert_or_404(alert_id)]
            if targets[0].status == "RESOLVED":
                raise ConflictError("a resolved alert keeps the triage it had when it was worked")
        else:
            targets, _ = self.repo.list_alerts(AlertFilter(statuses=["NEW", "TRIAGED", "INVESTIGATING", "ESCALATED"],
                                                           limit=100_000))
        now = utcnow()
        changed = 0
        for al in targets:
            ctx = dict(al.explanation.get("triage_context") or {})
            tr = tg.compute_triage(self._triage_inputs(al, ctx, now), self.cfg.triage)
            with self._unit():
                self.repo.update_alert(al.alert_id, triage_score=tr.score, triage_priority=tr.priority,
                                       triage_factors=tr.factors, triage_computed_at=now)
                if tr.priority != al.triage_priority:
                    changed += 1
                    self._alert_event(al.alert_id, actor, "priority_changed", al.status, al.status,
                                      previous=al.triage_priority, current=tr.priority, triage_score=tr.score,
                                      cause="recompute")
                    self._audit(actor, "alert_priority_changed", "monitoring_alert", al.alert_id,
                                previous=al.triage_priority, current=tr.priority, triage_score=tr.score)
        METRICS.inc("fira_triage_recomputed_total", len(targets))
        return {"recomputed": len(targets), "priority_changed": changed}

    def triage_explanation(self, alert_id: str) -> dict[str, Any]:
        a = self._alert_or_404(alert_id)
        return {"alert_id": a.alert_id, "score": a.triage_score, "priority": a.triage_priority,
                "computed_at": a.triage_computed_at, "factors": a.triage_factors,
                "thresholds": self.cfg.triage.thresholds,
                "method": "heuristic points over documented factors; not a probability of fraud",
                "max_total": sum(f[2] for f in tg.FACTORS)}

    # ------------------------------------------------------------------ transaction ingestion
    def _lookups(self) -> Any:
        from app.monitoring.ingest import Lookups

        st = self.store
        return Lookups(owners=st.owners_of_accounts, account_status=st.account_statuses,
                       known_devices=st.known_device_ids, known_merchants=st.known_merchant_ids,
                       existing_ids=st.known_transaction_ids,
                       recent_transactions=lambda accs, a, b: st.transactions_for_accounts(accs, a, b),
                       latest_timestamp=st.latest_transaction_time,
                       reference_time=lambda: max(utcnow(), st.as_of()))

    def ingest(self, rows: list[Any], actor: Principal | None, run_monitoring: bool = True, source: str = "api",
               expected: dict[str, Any] | None = None, require_transaction_id: bool = False) -> dict[str, Any]:
        """Validate a transaction batch through the data-quality gate, store the accepted rows, quarantine the
        rest with reason codes, record batch accounting, refresh the graph and optionally monitor."""
        from app.monitoring.ingest import missing_ids, validate_batch

        if not rows:
            raise ValueError("no transactions supplied")
        t0 = time.perf_counter()
        started = utcnow()
        batch_id = new_id("BAT")
        result = validate_batch(rows, self._lookups(), self.cfg.data_quality.to_rules(), require_transaction_id)
        failed, error = 0, None
        n = 0
        affected: list[str] = []
        if len(result.accepted):
            try:
                n = self.store.ingest_transactions(result.accepted)
            except Exception as exc:  # the whole accepted set failed to store: count it, never lose the accounting
                failed, error = len(result.accepted), f"storage failure: {type(exc).__name__}"
                log_event(log, "ingest storage failure", level=logging.ERROR, batch_id=batch_id,
                          operation="ingest", error=type(exc).__name__)
        if n:
            accs = sorted({str(x) for col in ("sender_account_id", "receiver_account_id")
                           for x in result.accepted[col].dropna()})
            affected = sorted(set(self.store.owners_of_accounts(accs).values()))
            self.c.rebuild_graph()
        received_ids = {str(r.get("transaction_id")) for r in rows if isinstance(r, dict) and r.get("transaction_id")}
        missing, missing_sample = missing_ids(expected, received_ids, self.store.known_transaction_ids, len(rows))
        expected_count = None
        if expected:
            if expected.get("count") is not None:
                expected_count = int(expected["count"])
            elif expected.get("ids"):
                expected_count = len(expected["ids"])
            elif expected.get("sequence"):
                s_ = expected["sequence"]
                expected_count = int(s_["end"]) - int(s_["start"]) + 1
        now = utcnow()
        batch = IngestionBatch(
            batch_id=batch_id, source=source[:60], status="FAILED" if failed else "COMPLETED", started_at=started,
            finished_at=now, created_by=actor.user_id if actor else None, expected_count=expected_count,
            received=len(rows), processed=n, rejected=len(result.rejected), duplicates=result.group_count("duplicate"),
            malformed=result.group_count("malformed"), late=len(result.late_ids) if n else 0, failed=failed,
            missing=missing, ids_generated=result.ids_generated if n else 0, customers_affected=len(affected),
            error=error, details={"reasons": result.reason_counts, "missing_sample": missing_sample,
                                  "late_sample": result.late_ids[:25] if n else []})
        rejected_rows = [RejectedRow(batch_id=batch_id, row_number=r["row"], transaction_id=r["transaction_id"],
                                     reason_code=r["code"], reason_group=r["group"], reason=r["reason"],
                                     payload=r["payload"], created_at=now) for r in result.rejected]
        with self._unit():
            self.repo.save_batch(batch, rejected_rows)
            self._audit(actor, "transactions_ingested", "ingestion_batch", batch_id, accepted=n,
                        rejected=len(result.rejected), failed=failed, customers_affected=len(affected), source=source)
        METRICS.inc("fira_ingest_rows_total", n, outcome="processed")
        METRICS.inc("fira_ingest_rows_total", len(result.rejected), outcome="rejected")
        METRICS.inc("fira_ingest_rows_total", failed, outcome="failed")
        METRICS.observe("fira_ingest_batch_ms", (time.perf_counter() - t0) * 1000)
        if failed:
            raise RuntimeError(f"ingestion failed after validation; batch {batch_id} recorded with failed={failed}")
        out: dict[str, Any] = {"accepted": n, "rejected": result.rejected[:100], "rejected_count": len(result.rejected),
                               "customers_affected": affected[:200], "customers_affected_count": len(affected),
                               "ingest_ms": int((time.perf_counter() - t0) * 1000),
                               "batch": batch.model_dump(mode="json")}
        if run_monitoring and affected:
            end = pd.to_datetime(result.accepted["timestamp"], utc=True).max().to_pydatetime()
            run = self.run(window_end=end, customer_ids=affected, actor=actor, mode="ingest")
            out["monitoring_run"] = run.model_dump(mode="json")
        return out

    def register_seed_batch(self, source: str, expected: int | None, received: int, processed: int,
                            actor: Principal | None = None, details: dict[str, Any] | None = None) -> IngestionBatch:
        """Record a bulk load (seed data) in the batch ledger. `expected` is only given when the source declares it
        (e.g. a manifest); otherwise it stays null so coverage is never computed from an invented denominator."""
        now = utcnow()
        b = IngestionBatch(batch_id=new_id("BAT"), source=source[:60], started_at=now, finished_at=now,
                           created_by=actor.user_id if actor else None, expected_count=expected, received=received,
                           processed=processed, rejected=received - processed,
                           missing=None if expected is None else max(expected - received, 0), details=details or {})
        with self._unit():
            self.repo.save_batch(b, [])
            self._audit(actor, "seed_batch_registered", "ingestion_batch", b.batch_id, processed=processed, source=source)
        return b

    # ------------------------------------------------------------------ data-quality read model
    def data_quality_summary(self) -> dict[str, Any]:
        totals = self.repo.quality_totals()
        recent, _ = self.repo.list_batches(limit=10)
        by_group: dict[str, int] = {}
        from app.monitoring.ingest import code_group

        for code, n in totals["reasons"].items():
            by_group[code_group(code)] = by_group.get(code_group(code), 0) + n
        return {"totals": totals, "by_group": by_group, "recent_batches": [b.model_dump(mode="json") for b in recent],
                "definitions": {
                    "coverage": "received / expected, over batches that declared an expected count",
                    "processing_success": "processed / received",
                    "quality_score": "100 x (processed - late) / received",
                    "late": "accepted rows older than the newest stored transaction by more than late_after_hours"}}

    def list_batches(self, limit: int, offset: int) -> dict[str, Any]:
        items, total = self.repo.list_batches(limit, offset)
        return {"total": total, "items": [b.model_dump(mode="json") for b in items]}

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        b = self.repo.get_batch(batch_id)
        if b is None:
            raise NotFoundError(f"batch {batch_id} not found")
        return b.model_dump(mode="json")

    def list_rejected(self, batch_id: str | None, reason_code: str | None, reason_group: str | None, limit: int,
                      offset: int) -> dict[str, Any]:
        items, total = self.repo.list_rejected(batch_id, reason_code, reason_group, limit, offset)
        return {"total": total, "items": [r.model_dump(mode="json") for r in items]}

    # ------------------------------------------------------------------ alert workflow
    def assign_alert(self, alert_id: str, assignee: str, actor: Principal) -> MonitoringAlert:
        a = self._alert_or_404(alert_id)
        if a.status == "RESOLVED":
            raise ConflictError("a resolved alert cannot be assigned")
        if not actor.has("admin"):
            if assignee != actor.user_id:
                raise AccessError("analysts can only assign alerts to themselves")
            if a.assigned_to not in (None, actor.user_id):
                raise AccessError(f"alert is assigned to {a.assigned_to}")
        else:
            self._require_user(assignee)
        with self._unit():
            upd = self.repo.update_alert(alert_id, assigned_to=assignee, updated_at=utcnow())
            assert upd is not None
            self._alert_event(alert_id, actor, "assigned", a.status, a.status, assignee=assignee, previous=a.assigned_to)
            self._audit(actor, "alert_assigned", "monitoring_alert", alert_id, assignee=assignee, previous=a.assigned_to)
        return upd

    def transition_alert(self, alert_id: str, to_status: str, actor: Principal, reason: str | None = None,
                         resolution: str | None = None) -> MonitoringAlert:
        a = self._alert_or_404(alert_id)
        if not self._can_act(actor, a.assigned_to):
            raise AccessError(f"alert is assigned to {a.assigned_to}")
        try:
            lc.check_alert_transition(a.status, to_status, resolution, reason)
        except lc.LifecycleError as e:
            raise ConflictError(str(e)) from None
        now = utcnow()
        fields: dict[str, Any] = {"status": to_status, "updated_at": now}
        if a.assigned_to is None:
            fields["assigned_to"] = actor.user_id  # working an unassigned alert claims it
        if to_status == "RESOLVED":
            fields.update(resolution=resolution, resolution_reason=reason, resolved_at=now, resolved_by=actor.user_id)
        with self._unit():
            upd = self.repo.update_alert(alert_id, **fields)
            assert upd is not None
            self._alert_event(alert_id, actor, "resolved" if to_status == "RESOLVED" else "status_changed", a.status,
                              to_status, reason=reason, resolution=resolution)
            self._audit(actor, "alert_status_changed", "monitoring_alert", alert_id, previous_status=a.status,
                        new_status=to_status, resolution=resolution)
            if to_status == "RESOLVED":
                self._audit(actor, "decision_recorded", "monitoring_alert", alert_id, decision=resolution,
                            reason=reason, previous_status=a.status, new_status=to_status, related_case=a.case_id)
        return upd

    def alert_detail(self, alert_id: str) -> dict[str, Any]:
        a = self._alert_or_404(alert_id)
        txns = []
        for tid in a.transaction_ids[:50]:
            t = self.store.get_transaction(tid)
            if t is not None:
                txns.append(t.model_dump(mode="json"))
        return {"alert": a, "transactions": txns, "events": self.repo.alert_events(alert_id)}

    # ------------------------------------------------------------------ case workflow
    def create_case(self, customer_id: str, alert_ids: list[str], actor: Principal, priority: str | None = None,
                    title: str | None = None) -> tuple[Case, bool]:
        """Create a case for the customer's alerts, or attach them to the customer's unclosed case."""
        if self.store.get_customer(customer_id) is None:
            raise NotFoundError(f"customer {customer_id} not found")
        alerts = [self._alert_or_404(i) for i in dict.fromkeys(alert_ids)]
        for a in alerts:
            if a.customer_id != customer_id:
                raise ValueError(f"alert {a.alert_id} belongs to {a.customer_id}, not {customer_id}")
            if a.status == "RESOLVED":
                raise ConflictError(f"alert {a.alert_id} is resolved and cannot join a case")
            if a.case_id:
                raise ConflictError(f"alert {a.alert_id} already belongs to case {a.case_id}")
            if not self._can_act(actor, a.assigned_to):
                raise AccessError(f"alert {a.alert_id} is assigned to {a.assigned_to}")
        existing = self.repo.find_open_case(customer_id)
        if existing is not None:
            if not self._can_act(actor, existing.assigned_to):
                raise AccessError(f"customer already has open case {existing.case_number} assigned to "
                                  f"{existing.assigned_to}")
            with self._unit():
                self._attach(existing, alerts, actor)
            return self.repo.get_case(existing.case_id) or existing, False
        now = utcnow()
        top = max((a.severity for a in alerts), key=lambda s: SEV_RANK[s], default="medium")
        case = Case(case_id=new_id("CASE"), customer_id=customer_id, status="OPEN",
                    priority=priority or self.cfg.cases.priority_by_severity.get(top, "medium"),  # type: ignore[arg-type]
                    title=title or self._title(customer_id, alerts), assigned_to=actor.user_id, opened_at=now,
                    created_at=now, updated_at=now, created_by=actor.user_id)
        try:
            with self._unit():
                case = self.repo.create_case(case)
                self._case_event(case.case_id, actor, "created", None, "OPEN", alerts=[a.alert_id for a in alerts])
                self._audit(actor, "case_created", "case", case.case_id, case_number=case.case_number,
                            customer_id=customer_id, alerts=[a.alert_id for a in alerts], priority=case.priority)
                self._attach(case, alerts, actor, announce=False)
        except DuplicateOpenCase:
            raise ConflictError("customer already has an unclosed case") from None
        return self.repo.get_case(case.case_id) or case, True

    @staticmethod
    def _title(customer_id: str, alerts: list[MonitoringAlert]) -> str:
        names = sorted({a.detector_id for a in alerts})
        return f"{customer_id}: " + (", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
                                     if names else "manual investigation")

    def _attach(self, case: Case, alerts: list[MonitoringAlert], actor: Principal, announce: bool = True) -> None:
        for a in alerts:
            to_status = "INVESTIGATING" if a.status in ("NEW", "TRIAGED") else a.status
            self.repo.update_alert(a.alert_id, case_id=case.case_id, status=to_status, updated_at=utcnow(),
                                   assigned_to=a.assigned_to or case.assigned_to or actor.user_id)
            self._alert_event(a.alert_id, actor, "case_linked", a.status, to_status, case_id=case.case_id,
                              case_number=case.case_number)
            if to_status != a.status:
                self._audit(actor, "alert_status_changed", "monitoring_alert", a.alert_id, previous_status=a.status,
                            new_status=to_status, via="case_created")
        if announce and alerts:
            self._case_event(case.case_id, actor, "alerts_attached", None, None, alerts=[a.alert_id for a in alerts])
            self._audit(actor, "case_alerts_attached", "case", case.case_id, alerts=[a.alert_id for a in alerts])
            self.repo.update_case(case.case_id, updated_at=utcnow())

    def assign_case(self, case_id: str, assignee: str, actor: Principal) -> Case:
        cs = self._case_or_404(case_id)
        if cs.status == "CLOSED":
            raise ConflictError("a closed case cannot be reassigned")
        if not actor.has("admin"):
            if assignee != actor.user_id:
                raise AccessError("analysts can only assign cases to themselves")
            if cs.assigned_to not in (None, actor.user_id):
                raise AccessError(f"case is assigned to {cs.assigned_to}")
        else:
            self._require_user(assignee)
        with self._unit():
            upd = self.repo.update_case(case_id, assigned_to=assignee, updated_at=utcnow())
            assert upd is not None
            self._case_event(case_id, actor, "assigned", cs.status, cs.status, assignee=assignee, previous=cs.assigned_to)
            self._audit(actor, "case_assigned", "case", case_id, assignee=assignee, previous=cs.assigned_to)
        return upd

    def set_case_priority(self, case_id: str, priority: str, reason: str, actor: Principal) -> Case:
        """Change a case's priority. Needs a reason; recorded in the case timeline and the audit trail."""
        cs = self._case_or_404(case_id)
        if cs.status == "CLOSED":
            raise ConflictError("a closed case cannot be changed")
        if not self._can_act(actor, cs.assigned_to):
            raise AccessError(f"case is assigned to {cs.assigned_to}")
        if priority not in ("low", "medium", "high", "critical"):
            raise ValueError("priority must be low, medium, high or critical")
        if not lc.reason_ok(reason):
            raise ConflictError(f"changing a priority requires a reason of at least {lc.MIN_REASON} characters")
        if priority == cs.priority:
            raise ConflictError(f"case priority is already {priority}")
        with self._unit():
            upd = self.repo.update_case(case_id, priority=priority, updated_at=utcnow())
            assert upd is not None
            self._case_event(case_id, actor, "priority_changed", cs.status, cs.status, previous=cs.priority,
                             current=priority, reason=reason.strip())
            self._audit(actor, "case_priority_changed", "case", case_id, previous=cs.priority, current=priority,
                        reason=reason.strip())
        return upd

    def transition_case(self, case_id: str, to_status: str, actor: Principal) -> Case:
        cs = self._case_or_404(case_id)
        if not self._can_act(actor, cs.assigned_to):
            raise AccessError(f"case is assigned to {cs.assigned_to}")
        if to_status == "ESCALATED":
            raise ConflictError("escalate a case by recording an ESCALATED decision with a reason")
        try:
            lc.check_case_transition(cs.status, to_status)
        except lc.LifecycleError as e:
            raise ConflictError(str(e)) from None
        fields: dict[str, Any] = {"status": to_status, "updated_at": utcnow()}
        if cs.assigned_to is None:
            fields["assigned_to"] = actor.user_id
        with self._unit():
            upd = self.repo.update_case(case_id, **fields)
            assert upd is not None
            self._case_event(case_id, actor, "status_changed", cs.status, to_status)
            self._audit(actor, "case_status_changed", "case", case_id, previous_status=cs.status, new_status=to_status)
        return upd

    def decide_case(self, case_id: str, decision: str, reason: str, actor: Principal) -> dict[str, Any]:
        cs = self._case_or_404(case_id)
        if not self._can_act(actor, cs.assigned_to):
            raise AccessError(f"case is assigned to {cs.assigned_to}; only the assignee or an admin can decide")
        try:
            new_status = lc.check_case_decision(cs.status, decision, reason)
        except lc.LifecycleError as e:
            raise ConflictError(str(e)) from None
        now = utcnow()
        fields: dict[str, Any] = {"status": new_status, "decision": decision, "decision_reason": reason.strip(),
                                  "decided_by": actor.user_id, "updated_at": now}
        if cs.assigned_to is None:
            fields["assigned_to"] = actor.user_id
        if new_status == "CLOSED":
            fields["closed_at"] = now
        with self._unit():  # case + its alerts + history commit together; audit entries follow the commit
            upd = self.repo.update_case(case_id, **fields)
            assert upd is not None
            self._case_event(case_id, actor, "decision_recorded", cs.status, new_status, decision=decision,
                             reason=reason.strip())
            self._audit(actor, "decision_recorded", "case", case_id, decision=decision, reason=reason.strip(),
                        previous_status=cs.status, new_status=new_status, related_case=case_id)
            if new_status == "CLOSED":
                self._audit(actor, "case_closed", "case", case_id, decision=decision, case_number=cs.case_number)
            affected = self._cascade_to_alerts(upd, decision, reason.strip(), actor)
        inv = self._sync_investigation(upd, decision, reason.strip(), actor)
        return {"case": self.repo.get_case(case_id), "alerts_updated": affected, "investigation": inv}

    def _cascade_to_alerts(self, case: Case, decision: str, reason: str, actor: Principal) -> list[str]:
        touched: list[str] = []
        for a in self.repo.alerts_for_case(case.case_id):
            if a.status == "RESOLVED":
                continue
            now = utcnow()
            if decision == "ESCALATED":
                if a.status == "ESCALATED":
                    continue
                self.repo.update_alert(a.alert_id, status="ESCALATED", updated_at=now)
                self._alert_event(a.alert_id, actor, "status_changed", a.status, "ESCALATED",
                                  reason=f"case {case.case_number} escalated: {reason}")
                self._audit(actor, "alert_status_changed", "monitoring_alert", a.alert_id, previous_status=a.status,
                            new_status="ESCALATED", via="case_decision", related_case=case.case_id)
            else:
                self.repo.update_alert(a.alert_id, status="RESOLVED", resolution=decision,
                                       resolution_reason=f"case {case.case_number}: {reason}", resolved_at=now,
                                       resolved_by=actor.user_id, updated_at=now)
                self._alert_event(a.alert_id, actor, "resolved", a.status, "RESOLVED", resolution=decision,
                                  reason=f"case {case.case_number}: {reason}")
                self._audit(actor, "alert_status_changed", "monitoring_alert", a.alert_id, previous_status=a.status,
                            new_status="RESOLVED", resolution=decision, via="case_decision", related_case=case.case_id)
            touched.append(a.alert_id)
        return touched

    def _sync_investigation(self, case: Case, decision: str, reason: str, actor: Principal) -> dict[str, Any] | None:
        """Record the equivalent existing decision on a linked, still-open investigation."""
        if not case.investigation_id:
            return None
        inv = self.store.get_investigation(case.investigation_id)
        if inv is None or inv.status not in ("pending_review", "in_progress"):
            return None
        mapped = INVESTIGATION_DECISION[decision]
        self.store.add_decision(HumanDecision(
            decision_id=new_id("DEC"), investigation_id=inv.investigation_id, decided_by=actor.user_id,
            decision=mapped, rationale=f"via case {case.case_number}: {reason}"[:4000], created_at=utcnow()))  # type: ignore[arg-type]
        status, conclusion = INVESTIGATION_EFFECT[mapped]
        upd = self.store.update_investigation(inv.investigation_id, status=status, conclusion=conclusion,
                                              closed_at=utcnow() if status == "closed" else None)
        self._audit(actor, "human_decision", "investigation", inv.investigation_id, decision=mapped,
                    via_case=case.case_id)
        return {"investigation_id": inv.investigation_id, "status": upd.status if upd else status,
                "decision": mapped}

    def add_note(self, case_id: str, body: str, actor: Principal) -> CaseNote:
        cs = self._case_or_404(case_id)
        if cs.status == "CLOSED":
            raise ConflictError("notes cannot be added to a closed case")
        if not self._can_act(actor, cs.assigned_to):
            raise AccessError(f"case is assigned to {cs.assigned_to}")
        text = body.strip()
        if not text:
            raise ValueError("note is empty")
        note = CaseNote(note_id=new_id("NOTE"), case_id=case_id, author=actor.user_id, body=text, created_at=utcnow())
        with self._unit():
            self.repo.add_note(note)
            self.repo.update_case(case_id, updated_at=note.created_at)
            self._case_event(case_id, actor, "note_added", None, None, note_id=note.note_id)
            self._audit(actor, "investigation_note_added", "case", case_id, note_id=note.note_id, length=len(text))
        return note

    def add_evidence(self, case_id: str, kind: str, ref: str, actor: Principal, note: str | None = None) -> CaseEvidence:
        """Attach a verified object to a case. Only objects that exist are accepted (no free-text facts)."""
        cs = self._case_or_404(case_id)
        if cs.status == "CLOSED":
            raise ConflictError("evidence cannot be added to a closed case")
        if not self._can_act(actor, cs.assigned_to):
            raise AccessError(f"case is assigned to {cs.assigned_to}")
        now = utcnow()
        ev: CaseEvidence
        if kind == "transaction":
            t = self.store.get_transaction(ref)
            if t is None:
                raise NotFoundError(f"transaction {ref} not found")
            owned = {a.account_id for a in self.store.accounts_for_customer(cs.customer_id)}
            if t.sender_account_id not in owned and t.receiver_account_id not in owned:
                raise ValueError(f"transaction {ref} does not involve an account of {cs.customer_id}")
            ev = CaseEvidence(evidence_id=new_id("CEV"), case_id=case_id, transaction_id=ref,
                              evidence_class="DATABASE_FACT", title=f"Transaction {ref}",
                              content={**t.model_dump(mode="json"), **({"analyst_note": note} if note else {})},
                              source="transactions table", added_by=actor.user_id, created_at=now)
        elif kind == "document":
            chunks = self.c.doc_repo.chunks([ref])
            if ref not in chunks:
                raise NotFoundError(f"document chunk {ref} not found")
            ch = chunks[ref]
            ev = CaseEvidence(evidence_id=new_id("CEV"), case_id=case_id, document_id=ch.get("document_id"),
                              chunk_id=ref, evidence_class="DOCUMENT_EVIDENCE",
                              title=f"{ch.get('section') or 'Document passage'}",
                              content={"excerpt": str(ch.get("text", ""))[:1500], "section": ch.get("section"),
                                       "page": ch.get("page"), **({"analyst_note": note} if note else {})},
                              source=f"document {ch.get('document_id')}", added_by=actor.user_id, created_at=now)
        elif kind == "alert":
            al = self._alert_or_404(ref)
            if al.case_id != case_id:
                raise ValueError(f"alert {ref} is not linked to this case")
            ev = CaseEvidence(evidence_id=new_id("CEV"), case_id=case_id, alert_id=ref,
                              evidence_class="RULE_RESULT", title=f"Detector result: {al.detector_id}",
                              content={"explanation": al.explanation, "severity": al.severity,
                                       "risk_contribution": al.risk_contribution,
                                       **({"analyst_note": note} if note else {})},
                              source=f"detector {al.detector_id}", added_by=actor.user_id, created_at=now)
        else:
            raise ValueError("kind must be transaction, document or alert")
        with self._unit():
            self.repo.add_case_evidence(ev)
            self.repo.update_case(case_id, updated_at=now)
            self._case_event(case_id, actor, "evidence_added", None, None, evidence_id=ev.evidence_id, kind=kind, ref=ref)
            self._audit(actor, "evidence_added", "case", case_id, evidence_id=ev.evidence_id,
                        evidence_class=ev.evidence_class, ref=ref)
        return ev

    def run_investigation(self, case_id: str, actor: Principal, lookback_days: int | None = None) -> dict[str, Any]:
        """Run the existing evidence-grounded agent for the case's customer and link the investigation."""
        cs = self._case_or_404(case_id)
        if cs.status == "CLOSED":
            raise ConflictError("case is closed")
        if not self._can_act(actor, cs.assigned_to):
            raise AccessError(f"case is assigned to {cs.assigned_to}")
        res = self.c.agent.run(f"Investigate customer {cs.customer_id} for case {cs.case_number}", actor,
                               subject={"type": "customer", "id": cs.customer_id}, lookback_days=lookback_days,
                               request_id=request_id_var.get(), investigation_id=cs.investigation_id)
        inv_id = res.get("investigation_id")
        if inv_id and inv_id != cs.investigation_id:
            with self._unit():
                self.repo.update_case(case_id, investigation_id=inv_id, updated_at=utcnow())
                self._case_event(case_id, actor, "investigation_linked", None, None, investigation_id=inv_id)
        self._audit(actor, "agent_investigate", "investigation", inv_id, episode_id=res.get("episode_id"),
                    engine=res.get("engine"), via_case=case_id, result=res.get("status"))
        return res

    # ------------------------------------------------------------------ queries
    def list_alerts(self, f: AlertFilter) -> tuple[list[MonitoringAlert], int]:
        return self.repo.list_alerts(f)

    # ------------------------------------------------------------------ money-mule indicators
    def mule_assessment(self, customer_id: str, end: Any = None) -> dict[str, Any]:
        if self.store.get_customer(customer_id) is None:
            raise NotFoundError(f"customer {customer_id} not found")
        end = pd.Timestamp(end).to_pydatetime() if end is not None else self.store.as_of()
        try:
            a = self.c.risk_engine.assess_customer(customer_id, end, self.cfg.mule.window_days, self.cfg.baseline_days)
        except EntityNotFound:
            a = None
        METRICS.inc("fira_mule_assessments_total")
        return ml.assess_mule(self.store, self.c.graph, a, customer_id, end, self.cfg.mule, self._flagged_of)

    def _flagged_of(self, customer_ids: list[str]) -> set[str]:
        """Customers (of those given) that carry an open monitoring alert or a seeded open alert."""
        out = {c for c, f in (self.c.graph.customer_flags(customer_ids).items() if self.c.graph is not None else []) if f}
        for cid in customer_ids:
            if cid in out:
                continue
            _, n = self.repo.list_alerts(AlertFilter(customer_id=cid, statuses=["NEW", "TRIAGED", "INVESTIGATING",
                                                                                "ESCALATED"], limit=1))
            if n:
                out.add(cid)
        return out

    def mule_suspects(self, limit: int = 25, min_band: str = "LOW") -> dict[str, Any]:
        """Rank customers that already carry a fund-flow alert by their mule indicator score."""
        order = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
        flow = {"FAN_IN", "FAN_OUT", "RAPID_PASS_THROUGH", "CIRCULAR_FLOW", "DORMANT_REACTIVATION", "DEVICE_SHARING"}
        items, _ = self.repo.list_alerts(AlertFilter(statuses=["NEW", "TRIAGED", "INVESTIGATING", "ESCALATED"], limit=5000))
        cands = sorted({a.customer_id for a in items if a.detector_id in flow})[: self.cfg.mule.candidate_limit]
        rows = []
        for cid in cands:
            r = self.mule_assessment(cid)
            if order[r["band"]] >= order[min_band]:
                rows.append({k: r[k] for k in ("customer_id", "score", "band", "fired", "inbound_usd", "outbound_usd")})
        rows.sort(key=lambda r: (-r["score"], r["customer_id"]))
        return {"examined": len(cands), "suspects": rows[:limit], "disclaimer": ml.DISCLAIMER,
                "scope": "customers with an open fund-flow alert (bounded by mule.candidate_limit)"}

    def mule_flow(self, customer_id: str, depth: int, days: int) -> dict[str, Any]:
        g = self.c.graph
        if g is None or not hasattr(g, "flow_subgraph"):
            raise ValueError("the configured graph backend does not support flow queries")
        if self.store.get_customer(customer_id) is None:
            raise NotFoundError(f"customer {customer_id} not found")
        end = self.store.as_of()
        res = g.flow_subgraph(customer_id, depth, end - timedelta(days=days))
        res.update(customer_id=customer_id, depth=depth, days=days,
                   note="account-to-account transfers; external beneficiaries are not graph nodes")
        return res

    def mule_patterns(self, days: int, min_degree: int, limit: int) -> dict[str, Any]:
        g = self.c.graph
        if g is None or not hasattr(g, "fan_patterns"):
            raise ValueError("the configured graph backend does not support pattern search")
        rows = g.fan_patterns(self.store.as_of() - timedelta(days=days), min_degree, limit)
        return {"days": days, "min_degree": min_degree, "patterns": rows,
                "note": "structural patterns only; read them with the mule indicators before drawing conclusions"}

    # ------------------------------------------------------------------ alert quality and investigator work
    def alert_quality(self, since: Any = None) -> dict[str, Any]:
        return ql.alert_quality(self.repo.all_alerts(), since)

    def feedback(self, limit: int = 200) -> list[dict[str, Any]]:
        return ql.feedback_rows(self.repo.all_alerts(), limit)

    def my_work(self, actor: Principal, recent_days: int = 7) -> dict[str, Any]:
        """The investigator's own queue: open alerts by triage score, high priority, overdue and recent outcomes.
        Overdue uses the review-time targets in `triage.overdue_hours` (assumed targets, not regulatory SLAs)."""
        now = utcnow()
        open_states = ["NEW", "TRIAGED", "INVESTIGATING", "ESCALATED"]
        mine, _ = self.repo.list_alerts(AlertFilter(statuses=open_states, assigned_to=actor.user_id,
                                                    sort="triage_score", order="desc", limit=500))
        targets = self.cfg.triage.overdue_hours

        def overdue(a: MonitoringAlert) -> bool:
            limit_h = targets.get(a.triage_priority or "LOW", targets["LOW"])
            return (now - a.triggered_at).total_seconds() / 3600.0 > limit_h

        since = now - timedelta(days=recent_days)
        decided = [a for a in self.repo.resolved_alerts(since=since, limit=500) if a.resolved_by == actor.user_id]
        cases, _ = self.repo.list_cases(CaseFilter(assigned_to=actor.user_id, statuses=["OPEN", "INVESTIGATING",
                                                                                         "ESCALATED", "PENDING_REVIEW"],
                                                   limit=200))

        def short(items: list[MonitoringAlert], n: int = 50) -> list[dict[str, Any]]:
            return [a.model_dump(mode="json", exclude={"triage_factors", "explanation"}) for a in items[:n]]

        return {
            "as_of": now.isoformat(), "recent_days": recent_days,
            "counts": {"open": len(mine), "high_priority": sum(1 for a in mine if a.triage_priority in ("CRITICAL", "HIGH")),
                       "overdue": sum(1 for a in mine if overdue(a)),
                       "escalated": sum(1 for a in mine if a.status == "ESCALATED"),
                       "confirmed_recent": sum(1 for a in decided if a.resolution == "CONFIRMED_SUSPICIOUS"),
                       "cleared_recent": sum(1 for a in decided if a.resolution in ("CLEARED", "FALSE_POSITIVE")),
                       "open_cases": len(cases)},
            "open_alerts": short(mine),
            "high_priority": short([a for a in mine if a.triage_priority in ("CRITICAL", "HIGH")]),
            "overdue": short([a for a in mine if overdue(a)]),
            "recently_escalated": short([a for a in mine if a.status == "ESCALATED"]),
            "recently_confirmed": short([a for a in decided if a.resolution == "CONFIRMED_SUSPICIOUS"]),
            "recently_cleared": short([a for a in decided if a.resolution in ("CLEARED", "FALSE_POSITIVE")]),
            "open_cases": [c.model_dump(mode="json") for c in cases[:50]],
            "overdue_targets_hours": targets,
            "overdue_note": "targets are configurable assumptions (monitoring_config.yaml: triage.overdue_hours)"}
