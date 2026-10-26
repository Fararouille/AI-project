"""Heading-aware, block-aware chunking.

Naive fixed-size chunking has three failure modes that this module avoids:

1. **Topic bleed.** A fixed window merges the end of one section with the start of
   another. Splitting on Markdown headings first means a chunk never spans topics.
2. **Lost context.** A section titled "Limits" under "Dashboards" is meaningless in
   isolation. The heading breadcrumb is prepended to the *indexed* text, which makes
   short sections retrievable, while the *displayed* body stays clean.
3. **Broken structure.** Bullets, table rows, and code fences get sliced mid-item.
   Chunking at Markdown block level keeps every unit intact and separately citable.

A block is a paragraph (consecutive prose lines joined, because Markdown authors
hard-wrap), a single list item, a single table row, or one fenced code block.
"""

from __future__ import annotations

import hashlib
import re

from .models import Chunk, Document
from .text import normalize

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_LIST_RE = re.compile(r"^([-*+]|\d+[.)])\s+")

DEFAULT_MAX_WORDS = 170
DEFAULT_OVERLAP_WORDS = 40


def split_sections(markdown: str) -> list[tuple[str, str]]:
    """Split a Markdown document into (heading breadcrumb, body) pairs."""
    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    body: list[str] = []
    breadcrumb = ""
    in_fence = False

    def flush() -> None:
        text = "\n".join(body).strip()
        # A heading with no prose under it is navigation, not content.
        if text and not all(_HEADING_RE.match(line) for line in text.splitlines()):
            sections.append((breadcrumb, text))

    for line in markdown.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            body.append(line)
            continue
        match = None if in_fence else _HEADING_RE.match(line)
        if match is None:
            body.append(line)
            continue

        flush()
        body = []
        level = len(match.group(1))
        stack.append((level, match.group(2).strip()))
        while len(stack) > 1 and stack[-2][0] >= level:
            stack.pop(-2)
        breadcrumb = " > ".join(title for _, title in stack)

    flush()
    return sections


def split_blocks(body: str) -> list[str]:
    """Group a section body into Markdown blocks.

    Wrapped prose becomes one block; list items and table rows stay individual so a
    retrieved chunk reads like a list rather than a run-on paragraph.
    """
    blocks: list[str] = []
    paragraph: list[str] = []
    code: list[str] | None = None

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            blocks.append(" ".join(paragraph))
            paragraph = []

    for raw in body.splitlines():
        line = raw.strip()

        if _FENCE_RE.match(line):
            flush_paragraph()
            if code is None:
                code = []
            else:
                blocks.append("\n".join(code))
                code = None
            continue

        if code is not None:
            code.append(raw.rstrip())
            continue

        if not line:
            flush_paragraph()
            continue

        if _LIST_RE.match(line) or line.startswith(("|", ">")):
            flush_paragraph()
            blocks.append(line)
            continue

        paragraph.append(line)

    flush_paragraph()
    if code:
        blocks.append("\n".join(code))
    return [block for block in blocks if block.strip()]


def _pack_blocks(
    blocks: list[str], max_words: int, overlap_words: int
) -> list[list[str]]:
    """Pack blocks into chunks under a word budget, with block-level overlap.

    Overlap repeats trailing blocks from the previous chunk when they fit inside
    the budget. That is coarser than word overlap but never splits a table row.
    """
    packed: list[list[str]] = []
    current: list[str] = []
    total = 0

    for block in blocks:
        words = block.split()
        if len(words) > max_words:
            if current:
                packed.append(current)
                current, total = [], 0
            for start in range(0, len(words), max(1, max_words - overlap_words)):
                packed.append([" ".join(words[start : start + max_words])])
                if start + max_words >= len(words):
                    break
            continue

        if current and total + len(words) > max_words:
            carry: list[str] = []
            budget = overlap_words
            for previous in reversed(current):
                size = len(previous.split())
                if size > budget:
                    break
                carry.insert(0, previous)
                budget -= size
            packed.append(current)
            current = list(carry)
            total = sum(len(item.split()) for item in carry)

        current.append(block)
        total += len(words)

    if current:
        packed.append(current)
    return packed


def _stable_id(*parts: str) -> str:
    digest = hashlib.blake2b("\x1f".join(parts).encode("utf-8"), digest_size=8).hexdigest()
    return digest


def _clean_heading(breadcrumb: str, title: str) -> str:
    """Drop the document title from the breadcrumb so citations do not read
    "Dashboards > Dashboards > Limits"."""
    heading = breadcrumb.strip()
    title = title.strip()
    if not heading or heading == title:
        return ""
    if heading.startswith(title + " > "):
        return heading[len(title) + 3 :]
    return heading


def chunk_document(
    doc: Document,
    max_words: int = DEFAULT_MAX_WORDS,
    overlap_words: int = DEFAULT_OVERLAP_WORDS,
) -> list[Chunk]:
    """Turn one document into retrievable chunks with stable, deterministic ids."""
    text = normalize(doc.text)
    sections = split_sections(text)
    if not sections:
        sections = [(doc.title, text)]

    chunks: list[Chunk] = []
    ordinal = 0
    for breadcrumb, body in sections:
        heading = _clean_heading(breadcrumb, doc.title)
        for block in _pack_blocks(split_blocks(body), max_words, overlap_words):
            body_text = "\n".join(block).strip()
            if not body_text:
                continue
            indexed = (
                f"{doc.title}\n{heading}\n{body_text}" if heading else f"{doc.title}\n{body_text}"
            )
            chunks.append(
                Chunk(
                    chunk_id=_stable_id(doc.doc_id, str(ordinal), body_text[:64]),
                    doc_id=doc.doc_id,
                    doc_title=doc.title,
                    heading=heading,
                    ordinal=ordinal,
                    body=body_text,
                    text=indexed,
                    source=doc.source,
                )
            )
            ordinal += 1
    return chunks
