"""Plain-text and Markdown loading.

Markdown is already the chunker's native format, so this loader only extracts a
title. The first level-one heading wins; otherwise the filename is title-cased.
"""

from __future__ import annotations

from pathlib import Path

from .base import LoadedDocument, title_from_filename


class MarkdownLoader:
    name = "markdown"
    suffixes = frozenset({".md", ".markdown", ".txt"})

    def load(self, path: Path) -> LoadedDocument:
        # utf-8-sig strips the BOM that Windows editors commonly prepend; without
        # it the first line is "\ufeff# Title" and the H1 goes undetected.
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return LoadedDocument(title=stripped[2:].strip(), text=text)
        return LoadedDocument(title=title_from_filename(path), text=text)
