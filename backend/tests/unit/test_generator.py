import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from app.synthetic.generator import SCENARIO_EXPECTED_SIGNALS, generate_dataset
from tests import support


class GeneratorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = support.small_dataset()
        rd = lambda n: pd.read_csv(cls.d / f"{n}.csv.gz")  # noqa: E731
        cls.customers, cls.accounts, cls.tx = rd("customers"), rd("accounts"), rd("transactions")
        cls.merchants, cls.devices, cls.idents = rd("merchants"), rd("devices"), rd("customer_identifiers")
        cls.labels = json.loads((cls.d / "scenario_labels.json").read_text(encoding="utf-8"))

    def test_referential_integrity(self):
        accs = set(self.accounts.account_id)
        self.assertTrue(set(self.accounts.customer_id) <= set(self.customers.customer_id))
        self.assertTrue(set(self.tx.sender_account_id.dropna()) <= accs)
        self.assertTrue(set(self.tx.receiver_account_id.dropna()) <= accs)
        self.assertTrue(set(self.tx.merchant_id.dropna()) <= set(self.merchants.merchant_id))
        self.assertTrue(set(self.tx.device_id.dropna()) <= set(self.devices.device_id))
        self.assertTrue(set(self.idents.customer_id) <= set(self.customers.customer_id))
        self.assertTrue(self.tx.transaction_id.is_unique)
        self.assertTrue((self.tx.amount > 0).all())
        self.assertFalse((self.tx.sender_account_id.isna() & self.tx.receiver_account_id.isna()).any())

    def test_labels_cover_scenarios_and_existing_entities(self):
        scen = {lb["scenario"] for lb in self.labels}
        self.assertTrue(set(SCENARIO_EXPECTED_SIGNALS) <= scen)
        self.assertIn("normal", scen)
        self.assertTrue({lb["entity_id"] for lb in self.labels} <= set(self.customers.customer_id))

    def test_pii_minimised(self):
        self.assertTrue(self.idents.value_hash.str.fullmatch(r"[0-9a-f]{64}").all())
        self.assertFalse(self.idents.masked_value.str.contains(r"user\d+@", regex=True).any())
        self.assertFalse(self.idents.masked_value.str.contains(r"\+\d{6,}", regex=True).any())

    def test_deterministic_for_seed(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            m1 = generate_dataset(Path(a), n_customers=200, seed=5, history_days=60)
            m2 = generate_dataset(Path(b), n_customers=200, seed=5, history_days=60)
            self.assertEqual(m1["counts"], m2["counts"])
            t1 = pd.read_csv(Path(a) / "transactions.csv.gz")
            t2 = pd.read_csv(Path(b) / "transactions.csv.gz")
            pd.testing.assert_frame_equal(t1, t2)


if __name__ == "__main__":
    unittest.main()
