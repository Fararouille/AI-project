"""Corpus loading and index lifecycle."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .chunking import DEFAULT_MAX_WORDS, DEFAULT_OVERLAP_WORDS, chunk_document
from .embeddings import Embedder, get_embedder
from .models import Document
from .retrieval import Index

SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt"}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "document"


def load_file(path: Path, root: Path | None = None) -> Document:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix in {".md", ".markdown"}:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return Document(
                    doc_id=_slug(path.stem),
                    title=stripped[2:].strip(),
                    text=text,
                    source=str(path.relative_to(root) if root else path),
                )
    return Document(
        doc_id=_slug(path.stem),
        title=path.stem.replace("-", " ").replace("_", " ").title(),
        text=text,
        source=str(path.relative_to(root) if root else path),
    )


def load_documents(root: str | Path) -> list[Document]:
    """Load every supported file under ``root``, sorted for reproducible chunk ids."""
    base = Path(root)
    if not base.exists():
        raise FileNotFoundError(f"corpus directory not found: {base}")
    files = sorted(
        path
        for path in base.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )
    if not files:
        raise ValueError(f"no supported files found under {base}")
    return [load_file(path, base) for path in files]


def build_index(
    root: str | Path,
    max_words: int = DEFAULT_MAX_WORDS,
    overlap_words: int = DEFAULT_OVERLAP_WORDS,
    embedder: Embedder | None = None,
) -> Index:
    documents = load_documents(root)
    chunks = []
    for document in documents:
        chunks.extend(chunk_document(document, max_words, overlap_words))
    return Index(chunks, embedder or get_embedder())


def save_index(index: Index, path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(index.to_dict(), indent=2), encoding="utf-8")
    return destination


def load_index(path: str | Path, embedder: Embedder | None = None) -> Index:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return Index.from_dict(payload, embedder or get_embedder())
