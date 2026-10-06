"""State machines for alerts and cases (pure functions, no I/O)."""
from __future__ import annotations

UNRESOLVED: tuple[str, ...] = ("NEW", "TRIAGED", "INVESTIGATING", "ESCALATED")

ALERT_TRANSITIONS: dict[str, set[str]] = {
    "NEW": {"TRIAGED", "INVESTIGATING", "ESCALATED", "RESOLVED"},
    "TRIAGED": {"INVESTIGATING", "ESCALATED", "RESOLVED"},
    "INVESTIGATING": {"ESCALATED", "RESOLVED"},
    "ESCALATED": {"INVESTIGATING", "RESOLVED"},
    "RESOLVED": set(),
}
RESOLUTIONS = ("CLEARED", "FALSE_POSITIVE", "CONFIRMED_SUSPICIOUS")

CASE_TRANSITIONS: dict[str, set[str]] = {
    "OPEN": {"INVESTIGATING", "CLOSED"},
    "INVESTIGATING": {"ESCALATED", "PENDING_REVIEW", "CLOSED"},
    "ESCALATED": {"INVESTIGATING", "PENDING_REVIEW", "CLOSED"},
    "PENDING_REVIEW": {"INVESTIGATING", "CLOSED"},
    "CLOSED": set(),
}
CASE_CLOSING_DECISIONS = ("CLEARED", "FALSE_POSITIVE", "CONFIRMED_SUSPICIOUS")
MIN_REASON = 5


class LifecycleError(ValueError):
    """An illegal transition or a missing required field."""


def _reason_ok(reason: str | None) -> bool:
    return bool(reason and len(reason.strip()) >= MIN_REASON)


def check_alert_transition(current: str, target: str, resolution: str | None = None,
                           reason: str | None = None) -> None:
    if target not in ALERT_TRANSITIONS:
        raise LifecycleError(f"unknown alert status {target}")
    if target not in ALERT_TRANSITIONS.get(current, set()):
        raise LifecycleError(f"alert cannot move from {current} to {target}")
    if target == "RESOLVED":
        if resolution not in RESOLUTIONS:
            raise LifecycleError(f"resolving an alert requires a resolution: {', '.join(RESOLUTIONS)}")
        if not _reason_ok(reason):
            raise LifecycleError(f"resolving an alert requires a reason of at least {MIN_REASON} characters")
    else:
        if resolution is not None:
            raise LifecycleError("a resolution is only valid together with status RESOLVED")
        if target == "ESCALATED" and not _reason_ok(reason):
            raise LifecycleError(f"escalating an alert requires a reason of at least {MIN_REASON} characters")


def check_case_transition(current: str, target: str) -> None:
    if target not in CASE_TRANSITIONS:
        raise LifecycleError(f"unknown case status {target}")
    if target not in CASE_TRANSITIONS.get(current, set()):
        raise LifecycleError(f"case cannot move from {current} to {target}")
    if target == "CLOSED":
        raise LifecycleError("close a case by recording a decision (CLEARED, FALSE_POSITIVE or CONFIRMED_SUSPICIOUS)")


def check_case_decision(current: str, decision: str, reason: str | None) -> str:
    """Validate a case decision and return the resulting case status."""
    if current == "CLOSED":
        raise LifecycleError("case is closed; decisions are no longer accepted")
    if not _reason_ok(reason):
        raise LifecycleError(f"a decision requires a reason of at least {MIN_REASON} characters")
    if decision in CASE_CLOSING_DECISIONS:
        if current == "OPEN":
            raise LifecycleError("start the investigation (status INVESTIGATING) before closing a case")
        return "CLOSED"
    if decision == "ESCALATED":
        if current == "ESCALATED":
            raise LifecycleError("case is already escalated")
        if "ESCALATED" not in CASE_TRANSITIONS[current]:
            raise LifecycleError(f"case cannot be escalated from {current}")
        return "ESCALATED"
    raise LifecycleError(f"unknown decision {decision}")


def reason_ok(reason: str | None) -> bool:
    return _reason_ok(reason)
