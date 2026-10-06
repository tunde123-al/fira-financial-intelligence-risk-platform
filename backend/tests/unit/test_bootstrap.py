"""Start-up seeding: the database decides, the local disk does not."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.db.bootstrap import database_is_seeded, seed_if_empty


class _Conn:
    def __init__(self, n):
        self.n = n

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, _stmt):
        return SimpleNamespace(scalar=lambda: self.n)


class _Engine:
    def __init__(self, n):
        self.n = n

    def connect(self):
        return _Conn(self.n)


class SeedIfEmptyTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())  # no manifest.json: what an ephemeral disk looks like after a restart
        self.s = SimpleNamespace(dataset_dir=self.dir, database_url="postgresql://x")
        self.calls = []

    def gen(self, path, n_customers):
        self.calls.append(("generate", n_customers))
        (Path(path)).mkdir(parents=True, exist_ok=True)
        (Path(path) / "manifest.json").write_text("{}")

    def load(self, url, path):
        self.calls.append(("load", str(path)))
        return {"transactions": 1}

    def test_populated_database_is_left_alone_even_without_dataset_files(self):
        self.assertTrue(database_is_seeded(_Engine(5)))
        self.assertEqual(seed_if_empty(_Engine(5), self.s, self.gen, self.load), "already_loaded")
        self.assertEqual(self.calls, [])
        self.assertFalse((self.dir / "manifest.json").exists())

    def test_empty_database_generates_missing_files_then_loads(self):
        self.assertFalse(database_is_seeded(_Engine(0)))
        self.assertEqual(seed_if_empty(_Engine(0), self.s, self.gen, self.load), "loaded")
        self.assertEqual([c[0] for c in self.calls], ["generate", "load"])

    def test_empty_database_with_existing_files_loads_without_regenerating(self):
        (self.dir / "manifest.json").write_text("{}")
        self.assertEqual(seed_if_empty(_Engine(0), self.s, self.gen, self.load), "loaded")
        self.assertEqual([c[0] for c in self.calls], ["load"])


if __name__ == "__main__":
    unittest.main()
