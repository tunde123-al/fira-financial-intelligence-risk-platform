import shutil
import unittest
from pathlib import Path

import numpy as np

from app.config import REPO_ROOT
from app.documents.chunking import chunk_blocks
from app.documents.classify import classify
from app.documents.parsers import parse_markdown
from app.documents.pipeline import parse_document
from app.retrieval.bm25 import BM25, tokenize
from app.retrieval.embeddings import LSAEmbedder
from app.retrieval.vector_store import MemoryVectorStore
from tests import support

DOCS = REPO_ROOT / "documents"


class ParsingTest(unittest.TestCase):
    def test_markdown_front_matter_and_sections(self):
        pr = parse_markdown("---\ntitle: T\ndoc_type: aml_policy\n---\n# Top\n\n## 1. A\n\npara one\n\n- item\n- item2\n")
        self.assertEqual(pr.metadata["doc_type"], "aml_policy")
        kinds = [b.kind for b in pr.blocks]
        self.assertEqual(kinds, ["heading", "heading", "paragraph", "list"])
        self.assertEqual(pr.blocks[-1].section_path, ["Top", "1. A"])

    def test_chunks_never_cross_sections_and_keep_provenance(self):
        doc = parse_document(DOCS / "procedures" / "transaction_monitoring_procedure.md", DOCS)
        chunks = chunk_blocks(doc.document_id, doc.blocks, max_chars=400)
        self.assertGreater(len(chunks), 5)
        for c in chunks:
            self.assertTrue(c.section)
            self.assertTrue(c.chunk_id.startswith(doc.document_id))
            self.assertLessEqual(len(c.text), 400 * 2 + 200)
        self.assertEqual(len({c.chunk_id for c in chunks}), len(chunks))

    def test_pdf_with_table(self):
        doc = parse_document(DOCS / "reports" / "fincrime_mi_report_q2_2026.pdf", DOCS)
        self.assertEqual(doc.page_count, 2)
        tables = [b for b in doc.blocks if b.kind == "table"]
        self.assertTrue(tables and "False positive rate" in tables[0].text)
        self.assertEqual(doc.doc_type, "financial_report")
        self.assertTrue(all(b.page in (1, 2) for b in doc.blocks))

    @unittest.skipUnless(shutil.which("tesseract"), "tesseract not installed")
    def test_scanned_pdf_uses_ocr(self):
        doc = parse_document(DOCS / "memos" / "card_testing_memo_scanned.pdf", DOCS)
        self.assertEqual(doc.ocr_pages, [1])
        text = " ".join(b.text for b in doc.blocks).lower()
        self.assertIn("card testing", text)

    def test_docx_headings_lists_tables(self):
        doc = parse_document(DOCS / "contracts" / "correspondent_banking_agreement_template.docx", DOCS)
        self.assertEqual(doc.doc_type, "contract")
        self.assertIn("table", {b.kind for b in doc.blocks})
        self.assertTrue(any(b.text.startswith("2. Financial Crime Obligations") for b in doc.blocks if b.kind == "heading"))

    def test_classifier_reports_confidence(self):
        t, conf, scores = classify("This agreement between the parties may be terminated; governing law applies.")
        self.assertEqual(t, "contract")
        self.assertGreater(conf, 0.5)
        self.assertEqual(classify("x", declared="aml_policy")[:2], ("aml_policy", 1.0))
        self.assertEqual(classify("zzz")[0], "unclassified")


class RetrievalUnitTest(unittest.TestCase):
    def test_bm25_ranks_matching_doc_first(self):
        b = BM25()
        b.add("a", "rapid pass-through of funds by money mules")
        b.add("b", "school fees and property deposits")
        self.assertEqual(b.search("mule funds")[0][0], "a")
        self.assertEqual(b.search("mule", allowed={"b"}), [])
        self.assertIn("mule", tokenize("Mules"))

    def test_lsa_embeddings_normalised(self):
        texts = ["money mule pass through", "card testing burst", "dormant account reactivation", "circular transfers"]
        e = LSAEmbedder.fit(texts, dim=8)
        v = e.embed(texts)
        self.assertEqual(v.shape[0], 4)
        self.assertTrue(np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-5))

    def test_memory_vector_store_filters_and_persistence(self):
        tmp = Path(support.make_settings().model_dir) / "vs_test.npz"
        vs = MemoryVectorStore(tmp)
        vs.recreate(2)
        vs.upsert(["x", "y"], np.array([[1.0, 0.0], [0.0, 1.0]]), [{"doc_type": "a"}, {"doc_type": "b"}])
        self.assertEqual(vs.search(np.array([1.0, 0.0]), 1)[0]["chunk_id"], "x")
        self.assertEqual(vs.search(np.array([1.0, 0.0]), 1, {"doc_type": "b"})[0]["chunk_id"], "y")
        self.assertEqual(MemoryVectorStore(tmp).count(), 2)


class HybridRetrievalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = support.container()

    def test_passages_have_full_provenance(self):
        ps = self.c.retriever.search("who may decide to file a suspicious transaction report", k=3)
        self.assertTrue(ps)
        for p in ps:
            for f in ("document_id", "chunk_id", "section", "source", "title"):
                self.assertTrue(getattr(p, f), f)
        self.assertEqual(ps[0].document_id, "POL-AML-001")

    def test_metadata_filter(self):
        ps = self.c.retriever.search("money mule", k=5, doc_types=["typology_guidance"])
        self.assertTrue(ps and all(p.doc_type == "typology_guidance" for p in ps))

    def test_benchmark_quality_floor(self):
        from app.evaluation.benchmarks import retrieval_benchmark

        r = retrieval_benchmark(self.c.retriever)
        self.assertGreaterEqual(r["hybrid"]["r@5"], 0.8)
        self.assertGreaterEqual(r["hybrid"]["mrr"], 0.8)


if __name__ == "__main__":
    unittest.main()
