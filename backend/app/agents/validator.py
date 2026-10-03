"""Claim validation (hallucination control).

A claim is accepted only if:
  1. it cites at least one evidence ref, and every cited ref exists;
  2. every number in the claim matches a number in the cited evidence
     (within rounding tolerance), every date matches a date in it, and every
     entity id (CUST-/ACC-/TXN-/DEV-/MER-/INV-) appears in it;
  3. it does not use accusatory or definitive-guilt language;
  4. it does not direct an autonomous consequential action (freeze/close/block/
     file a report ...) unless it explicitly defers to human/authorised approval.

Exception: a claim whose text is exactly an insufficient-evidence statement may
have no citations.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.schemas.domain import EvidenceItem

NUM_RE = re.compile(r"(?<![A-Za-z\-])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(%?)")
DATE_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
ID_RE = re.compile(r"\b(?:CUST|ACC|TXN|DEV|MER|INV|ALR)-[A-Z0-9-]+\b")
REF_RE = re.compile(r"\bE\d{1,4}\b")
ACCUSATORY = re.compile(
    r"\b(is|was|are|were)\s+(a\s+|an\s+)?(criminal|fraudster|money\s+launderer|terrorist|thief|guilty)\b"
    r"|\b(committed|perpetrated)\s+(fraud|a\s+crime|money\s+laundering|an?\s+offen[cs]e)\b"
    r"|\b(laundered|stole|embezzled)\b"
    r"|\bdefinitely\s+(fraud|suspicious|laundering)\b", re.I)
ACTION = re.compile(r"\b(freeze|close|block|suspend|terminate|deny|reject|exit|file)\b[^.]{0,60}"
                    r"\b(account|card|relationship|customer|loan|report|str|sar)\b", re.I)
DEFERS = re.compile(r"\b(human|analyst|investigator|authori[sz]ed|approval|approve|mlro|officer|decision)\b", re.I)
INSUFFICIENT = re.compile(r"^\s*insufficient evidence\b", re.I)


def _numbers_in(obj: Any, out: set[float], text_parts: list[str]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(float(obj))
        return
    if isinstance(obj, str):
        text_parts.append(obj)
        for m in NUM_RE.finditer(obj):
            try:
                out.add(float(m.group(1).replace(",", "")))
            except ValueError:
                pass
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            text_parts.append(str(k))
            _numbers_in(v, out, text_parts)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers_in(v, out, text_parts)


class EvidenceIndex:
    def __init__(self, items: list[EvidenceItem]):
        self.items = {e.ref: e for e in items}
        self._cache: dict[str, tuple[set[float], str]] = {}

    def facts(self, ref: str) -> tuple[set[float], str]:
        if ref not in self._cache:
            e = self.items[ref]
            nums: set[float] = set()
            parts: list[str] = [e.title, e.source_id]
            _numbers_in(e.content, nums, parts)
            _numbers_in(e.title, nums, parts)
            self._cache[ref] = (nums, " ".join(parts) + " " + json.dumps(e.content, default=str))
        return self._cache[ref]


def _number_supported(value: float, pct: bool, pool: set[float]) -> bool:
    candidates = [value]
    if pct:
        candidates.append(value / 100.0)
    for v in candidates:
        for p in pool:
            tol = max(abs(p) * 0.011, 0.6 if abs(p) >= 1 else 0.006)
            if abs(v - p) <= tol:
                return True
    return False


def validate_claim(claim: dict[str, Any], index: EvidenceIndex) -> dict[str, Any]:
    text = str(claim.get("text", "")).strip()
    cites = [c for c in claim.get("citations") or [] if isinstance(c, str)]
    # citations written inline as [E3] also count
    cites = list(dict.fromkeys(cites + REF_RE.findall(text)))
    reasons: list[str] = []
    if not text:
        reasons.append("empty claim")
    if INSUFFICIENT.match(text) and not cites:
        return {"supported": True, "reasons": [], "citations": []}
    if not cites:
        reasons.append("no citations")
    missing = [c for c in cites if c not in index.items]
    if missing:
        reasons.append(f"unknown evidence refs: {', '.join(missing)}")
    valid = [c for c in cites if c in index.items]
    pool: set[float] = set()
    blob = ""
    for c in valid:
        nums, txt = index.facts(c)
        pool |= nums
        blob += " " + txt
    body = REF_RE.sub("", text)
    if valid:
        for d in DATE_RE.findall(body):
            if d not in blob:
                reasons.append(f"date {d} not in cited evidence")
        for eid in ID_RE.findall(body):
            if eid not in blob:
                reasons.append(f"entity {eid} not in cited evidence")
        body_wo = ID_RE.sub("", DATE_RE.sub("", body))
        for m in NUM_RE.finditer(body_wo):
            raw, pct = m.group(1), m.group(2) == "%"
            try:
                v = float(raw.replace(",", ""))
            except ValueError:
                continue
            if not pct and v <= 10 and "." not in raw:
                # small counts ("3 signals") must still be grounded, but also accept
                # them when they are counts of cited items.
                if _number_supported(v, pct, pool) or v <= len(valid):
                    continue
            if not _number_supported(v, pct, pool):
                reasons.append(f"number {raw}{'%' if pct else ''} not found in cited evidence")
    if ACCUSATORY.search(text):
        reasons.append("accusatory or definitive-guilt language")
    if ACTION.search(text) and not DEFERS.search(text):
        reasons.append("directs a consequential action without deferring to human approval")
    return {"supported": not reasons, "reasons": reasons, "citations": valid}


def validate_claims(sections: dict[str, list[dict[str, Any]]], items: list[EvidenceItem]) -> dict[str, Any]:
    index = EvidenceIndex(items)
    kept: dict[str, list[dict[str, Any]]] = {}
    rejected: list[dict[str, Any]] = []
    total = 0
    for name, claims in sections.items():
        kept[name] = []
        for c in claims:
            total += 1
            v = validate_claim(c, index)
            if v["supported"]:
                kept[name].append({**c, "citations": v["citations"]})
            else:
                rejected.append({"section": name, "text": c.get("text"), "reasons": v["reasons"]})
    unsupported = len(rejected)
    return {"sections": kept, "rejected": rejected, "total_claims": total, "unsupported_claims": unsupported,
            "unsupported_rate": round(unsupported / total, 4) if total else 0.0}
