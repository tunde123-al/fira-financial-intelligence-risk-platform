"""Ground-truth labels must never reach detection, scoring, triage, the mule view or the workflow.

Only the data generators (which write labels), the loader (which stores them for the evaluator), the evaluation modules and one
admin route that feeds the evaluator may mention them. Everything the product does at run time must not.
"""
import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
ALLOWED = {
    "synthetic/generator.py", "synthetic/monitoring_benchmark.py", "db/loader.py", "evaluation/benchmarks.py",
    "evaluation/monitoring_eval.py", "evaluation/runner.py", "evaluation/improvement.py", "api/routes_admin.py",
    "monitoring/benchmark.py",  # performance benchmark: generates banks, never scores with labels
}
PATTERN = re.compile(r"scenario_labels|is_suspicious|load_labels|scenario_label|\bground_truth\b|MULE_TYPOLOGIES", re.I)


class LabelLeakageTest(unittest.TestCase):
    def test_runtime_modules_do_not_reference_labels(self):
        offenders = []
        for f in APP.rglob("*.py"):
            rel = f.relative_to(APP).as_posix()
            if rel in ALLOWED:
                continue
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if PATTERN.search(code):
                    offenders.append(f"{rel}:{n}: {line.strip()[:100]}")
        self.assertEqual(offenders, [])

    def test_the_modules_that_decide_things_are_explicitly_covered(self):
        decisive = ["risk/engine.py", "monitoring/detectors.py", "monitoring/triage.py", "monitoring/mule.py", "monitoring/service.py",
                    "monitoring/ingest.py", "monitoring/quality.py", "monitoring/governance.py", "graph/networkx_backend.py"]
        for rel in decisive:
            self.assertTrue((APP / rel).exists(), rel)
            self.assertNotIn(rel, ALLOWED)

    def test_triage_inputs_are_not_label_shaped(self):
        from app.monitoring.triage import TriageInputs

        fields = set(TriageInputs.__dataclass_fields__)
        self.assertFalse({"label", "scenario", "is_suspicious", "resolution", "outcome", "disposition"} & fields)


if __name__ == "__main__":
    unittest.main()
