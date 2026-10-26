from __future__ import annotations

from cite.chunking import chunk_document, split_blocks, split_sections
from cite.models import Document


def test_split_sections_builds_breadcrumbs() -> None:
    markdown = """
# Guide

Intro text.

## Install

Run the installer.

### Notes

Be careful.
"""
    sections = split_sections(markdown)
    headings = [heading for heading, _ in sections]
    assert headings == ["Guide", "Guide > Install", "Guide > Install > Notes"]


def test_split_sections_ignores_headings_inside_code_fences() -> None:
    markdown = """
# API

```bash
# this is a shell comment, not a heading
curl example
```

Real prose after.
"""
    sections = split_sections(markdown)
    assert [h for h, _ in sections] == ["API"]
    assert "# this is a shell comment" in sections[0][1]


def test_chunk_indexes_heading_breadcrumb_but_keeps_body_clean() -> None:
    doc = Document(
        doc_id="api",
        title="API Guide",
        text="# API Guide\n\n## Rate limits\n\nRequests are limited to 600 per minute.",
    )
    chunks = chunk_document(doc)
    assert len(chunks) == 1
    assert chunks[0].body.startswith("Requests are limited")
    assert "Rate limits" in chunks[0].text
    assert "Rate limits" not in chunks[0].body


def test_long_sections_overlap_and_cover_all_words() -> None:
    words = " ".join(f"w{i}" for i in range(400))
    doc = Document(doc_id="long", title="Long", text=words)
    chunks = chunk_document(doc, max_words=100, overlap_words=20)

    assert len(chunks) == 5
    assert all(len(chunk.body.split()) <= 100 for chunk in chunks)
    covered = set(range(400))
    seen: set[int] = set()
    for chunk in chunks:
        seen.update(int(token[1:]) for token in chunk.body.split())
    assert seen == covered

    # Consecutive chunks must share words, otherwise overlap is not happening.
    first = {token for token in chunks[0].body.split()}
    second = {token for token in chunks[1].body.split()}
    assert first & second


def test_split_blocks_joins_wrapped_prose() -> None:
    body = (
        "A dashboard is a grid of tiles. Each tile is a saved query\n"
        "rendered as a chart.\n\nA second paragraph."
    )
    assert split_blocks(body) == [
        (
            "A dashboard is a grid of tiles. "
            "Each tile is a saved query rendered as a chart."
        ),
        "A second paragraph.",
    ]


def test_split_blocks_keeps_list_items_and_table_rows_separate() -> None:
    body = """
Intro sentence.

- 50 tiles per dashboard
- 200 dashboards per project

| Type | Limit |
|---|---|
| Tiles | 50 |
"""
    blocks = split_blocks(body)
    assert blocks[0] == "Intro sentence."
    assert blocks[1] == "- 50 tiles per dashboard"
    assert blocks[2] == "- 200 dashboards per project"
    assert "| Type | Limit |" in blocks
    assert "|---|---|" in blocks


def test_split_blocks_preserves_code_fences() -> None:
    body = """
Run this:

```bash
curl -X POST https://example.test
# keep this comment
```

Done.
"""
    blocks = split_blocks(body)
    assert any("curl -X POST" in block and "# keep this comment" in block for block in blocks)
    assert blocks[-1] == "Done."


def test_chunk_ids_are_stable_and_unique() -> None:
    doc = Document(doc_id="d", title="D", text="# A\n\nalpha beta\n\n# B\n\ngamma delta")
    first = chunk_document(doc)
    second = chunk_document(doc)
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
    assert len({c.chunk_id for c in first}) == len(first)
