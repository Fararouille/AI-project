"""Loader contract shared by every file format.

A loader's only job is to turn one file into a title and a Markdown-ish body.
Chunking, ids and collection assignment stay in :mod:`cite.ingest`, so adding a
format never touches retrieval.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class LoadedDocument:
    """Format-independent output of a loader."""

    title: str
    text: str


class MissingLoaderDependency(RuntimeError):
    """Raised with an install hint when an optional parser is unavailable."""


@runtime_checkable
class Loader(Protocol):
    name: str
    suffixes: frozenset[str]

    def load(self, path: Path) -> LoadedDocument: ...


def title_from_filename(path: Path) -> str:
    return path.stem.replace("-", " ").replace("_", " ").title()
