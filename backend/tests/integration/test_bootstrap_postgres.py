"""Both start-up scenarios against a real PostgreSQL: empty database, then the same database after a 'restart'
(new empty local disk). Skipped unless DATABASE_URL points at PostgreSQL."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.integration.test_failure_modes_postgres import HAVE_PG, _ScratchDb


@unittest.skipUnless(HAVE_PG, "DATABASE_URL is not a PostgreSQL URL")
class BootstrapScenariosTest(unittest.TestCase):
    def test_empty_then_populated(self):
        from sqlalchemy import create_engine

        from app.db.bootstrap import database_is_seeded, seed_if_empty

        name, url = _ScratchDb.create(load_data=False)
        os.environ["SEED_CUSTOMERS"] = "60"
        try:
            eng = create_engine(url)
            self.assertFalse(database_is_seeded(eng))
            disk1 = Path(tempfile.mkdtemp())
            self.assertEqual(seed_if_empty(eng, SimpleNamespace(dataset_dir=disk1, database_url=url)), "loaded")  # scenario A
            self.assertTrue((disk1 / "manifest.json").exists())
            self.assertTrue(database_is_seeded(eng))
            with eng.connect() as conn:
                n = conn.exec_driver_sql("SELECT count(*) FROM transactions").scalar()
            disk2 = Path(tempfile.mkdtemp())  # scenario B: the disk was wiped, the database was not
            self.assertEqual(seed_if_empty(eng, SimpleNamespace(dataset_dir=disk2, database_url=url)), "already_loaded")
            self.assertEqual(list(disk2.iterdir()), [])  # nothing generated
            with eng.connect() as conn:
                self.assertEqual(conn.exec_driver_sql("SELECT count(*) FROM transactions").scalar(), n)  # nothing reloaded
            eng.dispose()
        finally:
            os.environ.pop("SEED_CUSTOMERS", None)
            _ScratchDb.drop(name)


if __name__ == "__main__":
    unittest.main()
