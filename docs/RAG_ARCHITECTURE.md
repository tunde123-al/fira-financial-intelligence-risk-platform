# Document intelligence and RAG

## Ingestion pipeline (`app/documents/pipeline.py`)

```
file ─▶ parse ─▶ OCR (pages without a text layer) ─▶ structure (headings → section paths, tables, lists)
     ─▶ classify (declared type, else keyword evidence + confidence) ─▶ metadata (version, effective date, jurisdiction)
     ─▶ chunk (never crosses a section; tables kept whole; overlap 120 chars)
     ─▶ embed ─▶ Qdrant (payload: document_id, doc_type, section, page, source, jurisdiction)
     └────────▶ repository (Postgres document_registry / document_chunks with FTS, or a JSON file)
```

| Format | Parser | Verified on |
|---|---|---|
| Markdown with YAML front matter | built-in | 8 policy documents |
| PDF (text layer, tables) | pdfplumber; tables rendered as Markdown tables, duplicate table text removed | MI report with a table, 2 pages |
| Scanned PDF | Tesseract OCR per page (pages with < 25 characters of text) | image-only memo |
| DOCX | python-docx (heading styles, list styles, tables, core properties) | contract template |
| complex layouts | **Docling**, when installed and `DOCUMENT_PARSER=docling` (Docker build arg `WITH_DOCLING=true`) | not exercised here |

Closed investigation summaries are also indexed as a `CASE-NOTES` document (`investigation_report`
type). Episodic memory uses them for semantic recall of similar cases.

Every retrieved passage carries **provenance**: `document_id`, `page`, `section`, `chunk_id`,
`source`, `title`, `doc_type`, plus its semantic and keyword ranks and scores.

## Retrieval

```
query ─┬─▶ semantic: embed → Qdrant top-20 (metadata filter on doc_type)
       └─▶ keyword: Postgres FTS (OR of stemmed terms, ts_rank_cd) or BM25 top-20
            ─▶ Reciprocal Rank Fusion (k=60) ─▶ top-k ─▶ hydrate text + provenance from the repository
```

In investigations, hybrid document retrieval is one of five evidence sources the agent combines:

| Source | Mechanism |
|---|---|
| structured SQL | tools over `DataStore` (profile, transactions, statistics) |
| metrics | risk engine |
| graph | graph tools |
| documents | hybrid retriever, filtered to policy/procedure types |
| history | episodic memory (SQL + semantic search over case notes) |

The agent never sends the database to the LLM. Queries come from a fixed **signal → query
playbook** (`agents/report.py::SIGNAL_PLAYBOOK`), at most 6 signals × 2 passages plus one general
reporting-standard query, deduplicated and capped at 12 passages.

## Embeddings

The default is **LSA**: TF-IDF over uni- and bigrams, then truncated SVD to 128 dimensions, L2
normalised. It is local, deterministic, needs no GPU and works offline, which is why it was chosen
for a stack that must run on a modest machine and in CI. LSA is fitted on the corpus, so ingestion
refits and **re-embeds every chunk** whenever the corpus changes, and the fitted model is stored
next to the index. Set `EMBEDDING_PROVIDER=openai` (any OpenAI-compatible `/embeddings` endpoint)
for neural embeddings.

## Measured quality (25 hand-labelled queries, `evaluation/retrieval_qrels.json`)

| mode | P@1 | P@3 | R@3 | R@5 | MRR |
|---|---|---|---|---|---|
| keyword | 1.00 | 0.65 | 0.89 | 0.95 | 1.00 |
| semantic (LSA) | 0.92 | 0.64 | 0.85 | 0.93 | 0.95 |
| hybrid (RRF) | 0.92 | 0.67 | 0.90 | 0.95 | 0.95 |

On this small, terminology-heavy corpus, keyword search is very strong and hybrid retrieval
improves recall@3. The queries were written by the same team that wrote the corpus, so treat these
numbers as a regression baseline, not a quality claim.

## Citation and grounding

Retrieved passages become evidence items (`source_type=document`) with the chunk id and text. The
report's document section and every narrative claim cite these refs. The validator rejects claims
that cite unknown refs or that contain numbers, dates or identifiers absent from the cited
evidence. See AGENT_ARCHITECTURE.md.
