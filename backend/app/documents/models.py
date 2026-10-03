"""Document intelligence data models."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

BlockKind = Literal["heading", "paragraph", "list", "table", "quote"]


class Block(BaseModel):
    kind: BlockKind
    text: str
    level: int = 0
    page: int | None = None
    section_path: list[str] = Field(default_factory=list)


class ParsedDocument(BaseModel):
    document_id: str
    title: str
    doc_type: str
    doc_type_confidence: float = 1.0
    source: str
    path: str
    sha256: str
    page_count: int | None = None
    parser: str
    ocr_pages: list[int] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    blocks: list[Block]


class Chunk(BaseModel):
    chunk_id: str
    document_id: str
    ordinal: int
    page: int | None
    section: str | None
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class Passage(BaseModel):
    """A retrieved chunk with full provenance and per-retriever scores."""

    chunk_id: str
    document_id: str
    title: str
    doc_type: str
    page: int | None
    section: str | None
    source: str
    text: str
    score: float
    semantic_rank: int | None = None
    keyword_rank: int | None = None
    semantic_score: float | None = None
    keyword_score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
