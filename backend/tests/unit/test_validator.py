import unittest
from datetime import datetime, timezone

from app.agents.validator import EvidenceIndex, validate_claim, validate_claims
from app.schemas.domain import EvidenceItem


def ev(ref, content, title="t", source_id="src"):
    return EvidenceItem(evidence_id=f"EV-{ref}", investigation_id="INV-1", ref=ref, source_type="metric",
                        source_id=source_id, evidence_type="x", title=title, content=content, confidence=0.9,
                        created_at=datetime(2026, 9, 30, tzinfo=timezone.utc))


ITEMS = [
    ev("E1", {"score": 54.2, "band": "high", "window_start": "2026-08-31T23:00:00+00:00", "n_signals": 5},
       source_id="customer:CUST-10686"),
    ev("E2", {"description": "21 distinct counterparties sent funds; USD 12,345.67 received", "ratio": 0.93,
              "transactions": ["TXN-00000042"]}),
]


class ValidatorTest(unittest.TestCase):
    def setUp(self):
        self.idx = EvidenceIndex(ITEMS)

    def check(self, text, cites):
        return validate_claim({"text": text, "citations": cites}, self.idx)

    def test_supported_numbers_ids_and_dates(self):
        r = self.check("Customer CUST-10686 scored 54.2 (high) from 2026-08-31 with 5 indicators.", ["E1"])
        self.assertTrue(r["supported"], r)
        r = self.check("21 counterparties sent USD 12,345.67; 93% left within a day (TXN-00000042).", ["E2"])
        self.assertTrue(r["supported"], r)

    def test_fabricated_number_rejected(self):
        r = self.check("The customer received USD 99,000.", ["E2"])
        self.assertFalse(r["supported"])
        self.assertIn("number 99,000", " ".join(r["reasons"]))

    def test_unknown_ref_and_no_citation(self):
        self.assertFalse(self.check("Score is 54.2.", ["E9"])["supported"])
        self.assertFalse(self.check("Score is 54.2.", [])["supported"])

    def test_inline_citations_count(self):
        self.assertTrue(self.check("Score is 54.2 [E1].", [])["supported"])

    def test_fabricated_entity_and_date(self):
        self.assertFalse(self.check("Funds went to ACC-123456.", ["E2"])["supported"])
        self.assertFalse(self.check("Activity began 2026-01-01.", ["E1"])["supported"])

    def test_accusatory_language_rejected(self):
        self.assertFalse(self.check("The customer laundered the funds received (score 54.2).", ["E1"])["supported"])
        self.assertFalse(self.check("The customer is a criminal.", ["E1"])["supported"])

    def test_autonomous_action_requires_human_deferral(self):
        self.assertFalse(self.check("Freeze the account immediately.", ["E1"])["supported"])
        self.assertTrue(self.check("Consider blocking the card, subject to authorised Fraud Operations approval.",
                                   ["E1"])["supported"])

    def test_insufficient_evidence_allowed_without_citation(self):
        self.assertTrue(self.check("Insufficient evidence: no device data was available.", [])["supported"])

    def test_section_validation_counts(self):
        v = validate_claims({"executive_summary": [{"text": "Score 54.2.", "citations": ["E1"]},
                                                  {"text": "Score 77.", "citations": ["E1"]}]}, ITEMS)
        self.assertEqual(v["total_claims"], 2)
        self.assertEqual(v["unsupported_claims"], 1)
        self.assertEqual(len(v["sections"]["executive_summary"]), 1)
        self.assertEqual(v["unsupported_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
