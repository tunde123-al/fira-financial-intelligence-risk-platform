"""The dataset audit gives the same answer from PostgreSQL as from the in-memory store on the same dataset."""
import unittest

from tests import support
from tests.integration.test_failure_modes_postgres import HAVE_PG, _ScratchDb, container_for


@unittest.skipUnless(HAVE_PG, "DATABASE_URL is not a PostgreSQL URL")
class DatasetQualityParityTest(unittest.TestCase):
    def test_same_counts_in_both_stores(self):
        name, url = _ScratchDb.create()
        try:
            pg = container_for(url).store.dataset_quality()
            mem = support.container().store.dataset_quality()
            for k in ("records_processed", "valid", "with_issues", "quality_score", "other_tables"):
                self.assertEqual(pg[k], mem[k], k)
            self.assertEqual({k: v["count"] for k, v in pg["checks"].items()}, {k: v["count"] for k, v in mem["checks"].items()})
        finally:
            _ScratchDb.drop(name)


if __name__ == "__main__":
    unittest.main()
