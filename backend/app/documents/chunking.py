"""Structure-aware chunking.

Chunks never cross a section boundary, so every chunk carries one unambiguous
section and page reference for citation. Long sections are split on block
boundaries (then sentences) with a small overlap. Tables are kept whole where
possible because splitting a table destroys its meaning.
"""
from __future__ import annotations

import hashlib
import re

from app.documents.models import Block, Chunk

SENT_RE = re.compile(r"(?<=[.!?])\s+")


def _split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    out, cur = [], ""
    for s in SENT_RE.split(text):
        if len(cur) + len(s) + 1 > max_chars and cur:
            out.append(cur.strip())
            cur = ""
        cur += s + " "
    if cur.strip():
        out.append(cur.strip())
    return out


def chunk_blocks(document_id: str, blocks: list[Block], max_chars: int = 900, overlap_chars: int = 120,
                 base_metadata: dict | None = None) -> list[Chunk]:
    groups: list[tuple[str | None, int | None, list[Block]]] = []
    for b in blocks:
        section = " > ".join(b.section_path) if b.section_path else None
        if b.kind == "heading":
            groups.append((section, b.page, []))
            continue
        if not groups or groups[-1][0] != section:
            groups.append((section, b.page, []))
        groups[-1][2].append(b)

    chunks: list[Chunk] = []
    for section, page, bl in groups:
        if not bl:
            continue
        pieces: list[tuple[str, int | None]] = []
        cur, cur_page = "", page
        for b in bl:
            parts = [b.text] if b.kind == "table" and len(b.text) <= max_chars * 2 else _split_long(b.text, max_chars)
            for part in parts:
                if cur and len(cur) + len(part) + 2 > max_chars:
                    pieces.append((cur, cur_page))
                    tail = cur[-overlap_chars:]
                    cur = tail[tail.find(" ") + 1:] + "\n\n" if overlap_chars else ""
                    cur_page = b.page or cur_page
                cur += part + "\n\n"
                cur_page = cur_page or b.page
        if cur.strip():
            pieces.append((cur, cur_page))
        heading = section.split(" > ")[-1] if section else ""
        for text, pg in pieces:
            body = text.strip()
            ordinal = len(chunks)
            cid = hashlib.sha256(f"{document_id}:{ordinal}:{body}".encode()).hexdigest()[:16]
            chunks.append(Chunk(chunk_id=f"{document_id}#{ordinal:03d}-{cid[:6]}", document_id=document_id,
                                ordinal=ordinal, page=pg, section=section,
                                text=(f"{heading}\n{body}" if heading and not body.startswith(heading) else body),
                                metadata=dict(base_metadata or {})))
    return chunks
