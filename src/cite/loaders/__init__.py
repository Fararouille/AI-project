"""File-format registry.

``SUPPORTED_SUFFIXES`` is derived from the registered loaders, so adding a format
is one class plus one tuple entry rather than a second list to keep in sync.
"""

from __future__ import annotations

from pathlib import Path

from .base import LoadedDocument, Loader, MissingLoaderDependency
from .html import HTMLLoader
from .markdown import MarkdownLoader
from .pdf import PDFLoader

LOADERS: tuple[Loader, ...] = (MarkdownLoader(), HTMLLoader(), PDFLoader())

SUPPORTED_SUFFIXES: frozenset[str] = frozenset(
    suffix for loader in LOADERS for suffix in loader.suffixes
)


def get_loader(path: str | Path) -> Loader:
    suffix = Path(path).suffix.lower()
    for loader in LOADERS:
        if suffix in loader.suffixes:
            return loader
    supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
    raise ValueError(f"{path}: unsupported file type {suffix!r}; supported: {supported}")


def load_document(path: str | Path) -> LoadedDocument:
    return get_loader(path).load(Path(path))


__all__ = [
    "LOADERS",
    "SUPPORTED_SUFFIXES",
    "LoadedDocument",
    "MissingLoaderDependency",
    "get_loader",
    "load_document",
]
