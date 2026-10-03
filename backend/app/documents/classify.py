"""Document classification and metadata extraction.

Classification is deliberately transparent: declared metadata (front matter /
document properties) wins; otherwise a keyword-evidence scorer assigns the type
and reports its confidence and the matched terms. An LLM is not needed for this.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

DOC_TYPES: dict[str, list[str]] = {
    "aml_policy": ["money laundering", "terrorist financing", "mlro", "suspicious transaction", "tipping off",
                   "aml", "cft", "red flag"],
    "kyc_policy": ["know your customer", "due diligence", "kyc", "beneficial owner", "identity", "enhanced due",
                   "politically exposed"],
    "internal_procedure": ["procedure", "playbook", "investigator", "triage", "steps", "service level", "standard"],
    "typology_guidance": ["typolog", "mule", "indicator", "network"],
    "regulatory_document": ["regulation", "shall", "competent authority", "pursuant", "article", "directive", "act "],
    "financial_report": ["revenue", "quarter", "balance sheet", "net income", "profit", "total assets",
                         "management information", "volumes", "report q"],
    "internal_memo": ["memo", "subject:", "interim guidance", "fraud operations has observed"],
    "investigation_report": ["investigation", "subject", "finding", "conclusion", "case", "analyst"],
    "contract": ["agreement", "party", "parties", "hereby", "termination", "governing law", "obligations",
                 "indemnif"],
}


def classify(text: str, declared: str | None = None, title: str = "") -> tuple[str, float, dict[str, int]]:
    """Keyword-evidence classifier; terms found in the title count three times."""
    if declared:
        return declared, 1.0, {}
    low = text.lower()
    tlow = title.lower()
    scores: Counter[str] = Counter()
    for t, terms in DOC_TYPES.items():
        for term in terms:
            scores[t] += low.count(term) + 3 * tlow.count(term)
    if not scores or max(scores.values()) == 0:
        return "unclassified", 0.0, {}
    (best, top), *rest = scores.most_common()
    second = rest[0][1] if rest else 0
    confidence = round(top / (top + second), 3) if top + second else 0.0
    return best, confidence, dict(scores)


VERSION_RE = re.compile(r"\bversion[:\s]+v?(\d+(?:\.\d+)*)", re.I)
DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
EFFECTIVE_RE = re.compile(r"effective(?:\s+date)?[:\s]+(20\d{2}-\d{2}-\d{2})", re.I)


def extract_metadata(text: str, declared: dict[str, Any]) -> dict[str, Any]:
    meta = {k: (str(v) if not isinstance(v, (int, float, bool)) else v) for k, v in declared.items() if v is not None}
    if "version" not in meta:
        m = VERSION_RE.search(text)
        if m:
            meta["version"] = m.group(1)
    if "effective_date" not in meta:
        m = EFFECTIVE_RE.search(text) or DATE_RE.search(text)
        if m:
            meta["effective_date"] = m.group(1)
    meta.setdefault("jurisdiction", "unspecified")
    return meta
