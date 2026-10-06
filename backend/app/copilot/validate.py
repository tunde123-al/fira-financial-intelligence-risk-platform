"""Validation of untrusted inputs and of the model's draft interpretation.

Two directions of distrust:

* **Into the model.** Database strings are *data*, not instructions. Anything that goes into a statement or the model prompt and
  comes from a free-text-capable column is reduced to an identifier-like token (`safe_token`); values that do not fit are replaced
  by a fixed placeholder, so an attacker-controlled value such as "IGNORE ALL PREVIOUS INSTRUCTIONS" cannot reach the prompt.
* **Out of the model.** The model's paragraph is accepted only if it passes every check in `check_interpretation`. A draft that
  fails is dropped and the deterministic template is used. The structured sections (facts, signals, factors, risk level,
  recommendations) never come from the model, so even a fully compromised provider can only influence one labelled paragraph,
  and that paragraph is constrained here.

None of this is a proof of safety: the checks are rule-based and a sufficiently careful paraphrase can pass them (see
docs/AI_THREAT_MODEL.md). They reduce risk; the architecture (deterministic sections, labelled AI text, human review) is the control.
"""
from __future__ import annotations

import re
from typing import Any

PLACEHOLDER = "[unrecognised value]"
MAX_WORDS = 150
MAX_CHARS = 1200

# --------------------------------------------------------------------------- inputs
IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,39}$")
COUNTRY = re.compile(r"^[A-Z]{2}$")
STATUS_WORD = re.compile(r"^[a-z][a-z_]{1,24}$")


def safe_token(value: Any, pattern: re.Pattern[str] = IDENT) -> str:
    """Return `value` only if it is a short identifier-like token; otherwise a fixed placeholder."""
    s = "" if value is None else str(value).strip()
    return s if pattern.fullmatch(s) else PLACEHOLDER


def safe_text(value: Any, limit: int = 400) -> str:
    """For engine-produced prose: strip control characters, collapse whitespace, cap length."""
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", "" if value is None else str(value))
    return re.sub(r"\s+", " ", s).strip()[:limit]


# --------------------------------------------------------------------------- outputs
_ID_PREFIX = r"(?:CUST|CUSTOMER|ACC|ACCOUNT|TXN|TRANSACTION|MAL|ALR|ALERT|CASE|INV|INVESTIGATION|DEV|DEVICE|MER|MERCHANT|BAT)"
_ABBREV = r"(?:CUST|ACC|TXN|MAL|ALR|CASE|INV|DEV|MER|BAT)"
# 1. canonical form with a hyphen: any alphanumeric body, because real ids are not all hex (INV-ZZZ999, MAL-ABCDEF123456)
ID_STRICT = re.compile(rf"\b({_ABBREV})-((?:(?=[0-9A-Z]*\d)|(?=[A-F]{{6,}}\b))[0-9A-Z]{{2,39}})\b", re.I)
# 2. tolerant spellings: "cust 10226", "CUST_10226", "CUST10226", "Customer #10226", "TXN 12345678". The id part must contain
#    a digit, otherwise ordinary words after an entity word ("account activity", "case study") would be flagged.
ID_LOOSE = re.compile(rf"\b({_ID_PREFIX})[\s_#:]*((?=[0-9A-Z]*\d)[0-9A-Z]{{3,16}})\b", re.I)
CANON = {"CUSTOMER": "CUST", "ACCOUNT": "ACC", "TRANSACTION": "TXN", "ALERT": "ALR", "INVESTIGATION": "INV", "DEVICE": "DEV",
         "MERCHANT": "MER"}

ACCUSE = re.compile(
    r"\b(launder(?:s|ed|ing|er)?|fraudster|fraudulent|criminal|guilty|illegal(?:ly)?|stole|stolen|embezzl\w+|terroris\w+|"
    r"(?:is|are|was|were|being)\s+(?:a\s+)?(?:money\s+)?mules?|committed|committing|perpetrat\w+|swindl\w+|crook\w*|"
    r"engaged in (?:money laundering|fraud|crime)|involved in (?:money laundering|fraud|crime))\b", re.I)
HEDGE = re.compile(
    r"\b(may|might|could|possibly|possible|potential(?:ly)?|indicators?|indicat\w+|suggest\w*|consistent with|requires? "
    r"(?:human )?review|further review|should be reviewed|elevated risk|not (?:proof|evidence of)|no evidence|"
    r"does not (?:establish|prove)|cannot (?:determine|conclude)|unverified|alleged)\b", re.I)
CERTAINTY = re.compile(r"\b(definitely|certainly|clearly|undoubtedly|proves?|proven|confirmed|without (?:a )?doubt|obviously|"
                       r"it is certain)\b", re.I)
DIRECTIVE = re.compile(
    r"\b(freeze|block|close|terminate|suspend|seize|arrest|prosecute|blacklist)\b[^.]{0,40}\b(account|customer|card|funds|him|her|them)\b|"
    r"\bfile (?:a )?(?:SAR|STR|suspicious activity report)\b|\breport (?:him|her|them|the customer) to (?:the )?(?:police|authorities|regulator)\b|"
    r"\bdeny (?:credit|the loan)\b", re.I)
LOW_RISK_CLAIM = re.compile(
    r"\b(low[- ]risk|no suspicious (?:activity|indicators?|behaviou?r)|nothing suspicious|no (?:risk|concerns?|red flags?)|"
    r"not (?:suspicious|risky|a risk)|is safe|appears (?:legitimate|benign)|no further (?:action|review) (?:is )?(?:needed|required))\b", re.I)
HIGH_RISK_CLAIM = re.compile(r"\b(high[- ]risk|critical[- ]risk|severe risk|highly suspicious)\b", re.I)
EXFIL = re.compile(r"(https?://|www\.|\bftp://|<[a-z/!][^>]*>|```|\]\(|\bdata:[a-z/]+;|[\w.+-]+@[\w-]+\.[\w.]+|\bjavascript:)", re.I)
ECHO = re.compile(r"\b(ignore (?:all )?(?:the )?(?:previous|above|prior) (?:instructions|messages)|system (?:message|prompt)|"
                  r"as an ai|developer mode|jailbreak|you are now|new instructions)\b", re.I)
NUMBER_SCORE = re.compile(r"\b(?:risk )?score(?: of| is| was|:)?\s*(\d{1,3}(?:\.\d+)?)\b", re.I)


def canonical_ids(text: str) -> set[str]:
    out = set()
    for rx in (ID_STRICT, ID_LOOSE):
        for m in rx.finditer(text):
            prefix = m.group(1).upper()
            out.add(f"{CANON.get(prefix, prefix)}-{m.group(2).upper()}")
    return out


def _allowed_canonical(ids: set[str]) -> set[str]:
    out = set()
    for i in ids:
        m = re.match(rf"^({_ID_PREFIX})[\-_]?([0-9A-Z][0-9A-Z-]*)$", i, re.I)
        out.add(f"{CANON.get(m.group(1).upper(), m.group(1).upper())}-{m.group(2).upper()}" if m else i.upper())
    return out


def check_interpretation(text: str, allowed_ids: set[str], risk: dict[str, Any] | None) -> tuple[bool, str]:
    """Returns (accepted, reason_code). `risk` is {"score": float, "band": str, "flagged": bool} from the deterministic engine."""
    if not text or not text.strip():
        return False, "empty"
    if len(text) > MAX_CHARS or len(text.split()) > MAX_WORDS:
        return False, "too_long"
    if EXFIL.search(text):
        return False, "markup_or_link"
    if ECHO.search(text):
        return False, "instruction_echo"
    if canonical_ids(text) - _allowed_canonical(allowed_ids):
        return False, "unknown_entity"
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if ACCUSE.search(sentence) and (not HEDGE.search(sentence) or CERTAINTY.search(sentence)):
            return False, "unqualified_accusation"
        if CERTAINTY.search(sentence) and re.search(r"\b(fraud|crime|criminal|launder\w*|illegal|mule|suspicious)\b", sentence, re.I):
            return False, "overstated_certainty"
    if DIRECTIVE.search(text):
        return False, "action_directive"
    if risk is not None:
        band, flagged = str(risk.get("band", "")).lower(), bool(risk.get("flagged"))
        if LOW_RISK_CLAIM.search(text) and (flagged or band in ("high", "critical")):
            return False, "contradicts_risk_level"
        if HIGH_RISK_CLAIM.search(text) and band == "low" and not flagged:
            return False, "contradicts_risk_level"
        for m in NUMBER_SCORE.finditer(text):
            if abs(float(m.group(1)) - float(risk.get("score", -1))) > 0.15:
                return False, "wrong_number"
    return True, "ok"


def accusation_in_question(question: str) -> bool:
    """The investigator is asking a verdict question: the answer must say FIRA cannot give a verdict."""
    return bool(re.search(r"\b(launder\w*|fraud\w*|criminal|guilty|illegal|crime|committed|terroris\w+|is (?:he|she|it|this \w+) (?:a )?mule)\b",
                          question, re.I))
