"""Evidence classes: where a piece of evidence came from.

An investigator must be able to tell a database fact from a rule result, a graph result, a retrieved
document passage and AI-written prose. The LLM is never the source of a fact: its output is only ever
classified as LLM_GENERATED_SUMMARY and is built from already-stored evidence.
"""
from __future__ import annotations

from typing import Any

DATABASE_FACT = "DATABASE_FACT"
RULE_RESULT = "RULE_RESULT"
GRAPH_RESULT = "GRAPH_RESULT"
DOCUMENT_EVIDENCE = "DOCUMENT_EVIDENCE"
LLM_SUMMARY = "LLM_GENERATED_SUMMARY"

CLASSES = (DATABASE_FACT, RULE_RESULT, GRAPH_RESULT, DOCUMENT_EVIDENCE, LLM_SUMMARY)
LABELS = {
    DATABASE_FACT: "Database fact", RULE_RESULT: "Rule result", GRAPH_RESULT: "Graph result",
    DOCUMENT_EVIDENCE: "Document evidence", LLM_SUMMARY: "AI-generated summary",
}
# existing EvidenceItem.source_type -> class
SOURCE_TYPE_CLASS = {
    "database": DATABASE_FACT, "history": DATABASE_FACT, "metric": RULE_RESULT, "ml": RULE_RESULT,
    "graph": GRAPH_RESULT, "document": DOCUMENT_EVIDENCE,
}


def classify_source_type(source_type: str) -> str:
    return SOURCE_TYPE_CLASS.get(source_type, DATABASE_FACT)


def classify_item(item: dict[str, Any]) -> dict[str, Any]:
    """Add `evidence_class` and its label to an investigation evidence item (dict form)."""
    cls = classify_source_type(str(item.get("source_type", "")))
    return {**item, "evidence_class": cls, "evidence_class_label": LABELS[cls], "ai_generated": False}


def classify_claims(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Narrative claims are not evidence. Mark which were written by an LLM so the UI can label them."""
    out = []
    for c in claims or []:
        ai = str(c.get("source", "deterministic")) == "llm"
        out.append({**c, "ai_generated": ai,
                    "evidence_class": LLM_SUMMARY if ai else None,
                    "generated_by": "llm" if ai else "deterministic"})
    return out
