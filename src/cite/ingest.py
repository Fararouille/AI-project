"""Corpus loading, incremental indexing, and atomic index lifecycle.

The first version of ``cite`` rebuilt every chunk and every vector on every
ingest. That is fine for 14 documents and wrong for 5,000: a one-line edit would
re-embed the entire corpus and cost real money with a hosted embedder.

The index now carries a manifest keyed by document id:

* a SHA-256 of the document's title and normalized text
* the ids of the chunks produced from it

On the next run, unchanged documents are copied verbatim - chunks and vectors -
and only new or edited documents are chunked and embedded. The rebuilt index is
written to a temporary file and swapped in with :func:`os.replace`, so a crash
mid-write cannot leave a half-valid index behind.

A manifest is only reusable when the embedder and chunking parameters match. If
they do not, the caller gets a full rebuild rather than a silently mixed vector
space.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .chunking import DEFAULT_MAX_WORDS, DEFAULT_OVERLAP_WORDS, chunk_document
from .embeddings import Embedder, get_embedder
from .loaders import SUPPORTED_SUFFIXES, load_document
from .models import Document
from .retrieval import Index
from .store import is_sqlite_path
from .text import normalize

MANIFEST_VERSION = 1


class EmbedderMismatchError(ValueError):
    """The index was built with a different embedder than the one requested."""


@dataclass(frozen=True, slots=True)
class BuildStats:
    """What an ingest actually did. Printed by the CLI and returned by the API."""

    collection: str
    documents: int
    documents_added: int
    documents_changed: int
    documents_removed: int
    documents_unchanged: int
    chunks: int
    chunks_reused: int
    chunks_embedded: int
    full_rebuild: bool
    reason: str
    elapsed_ms: float
    # Document ids, so a SQLite store can upsert only what changed. They stay out
    # of ``as_dict`` because a 5,000-document response does not need them.
    added_doc_ids: tuple[str, ...] = ()
    changed_doc_ids: tuple[str, ...] = ()
    removed_doc_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "collection": self.collection,
            "documents": self.documents,
            "documents_added": self.documents_added,
            "documents_changed": self.documents_changed,
            "documents_removed": self.documents_removed,
            "documents_unchanged": self.documents_unchanged,
            "chunks": self.chunks,
            "chunks_reused": self.chunks_reused,
            "chunks_embedded": self.chunks_embedded,
            "full_rebuild": self.full_rebuild,
            "reason": self.reason,
            "elapsed_ms": round(self.elapsed_ms, 2),
        }


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "document"


def _fingerprint(document: Document) -> str:
    payload = f"{document.title}\x00{normalize(document.text)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_file(path: Path, root: Path | None = None) -> Document:
    loaded = load_document(path)
    if not loaded.text.strip():
        raise ValueError(f"{path}: no extractable text; empty files are not indexable")
    source = str(path.relative_to(root) if root else path).replace("\\", "/")
    return Document(
        doc_id=_slug(path.stem),
        title=loaded.title,
        text=loaded.text,
        source=source,
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
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise ValueError(f"no supported files found under {base} (supported: {supported})")

    documents: list[Document] = []
    seen: set[str] = set()
    for path in files:
        document = load_file(path, base)
        if document.doc_id in seen:
            # Two directories can contain "overview.md". Keep both instead of
            # letting one silently overwrite the other in the manifest.
            digest = hashlib.blake2b(str(path).encode("utf-8"), digest_size=3).hexdigest()
            document = replace(document, doc_id=f"{document.doc_id}-{digest}")
        seen.add(document.doc_id)
        documents.append(document)
    return documents


def _reuse_reason(
    previous: Index | None,
    embedder: Embedder,
    max_words: int,
    overlap_words: int,
    collection: str,
) -> tuple[bool, str]:
    if previous is None:
        return False, "no previous index"
    if not previous.manifest:
        return False, "previous index has no manifest (format v1)"
    if previous.manifest.get("collection") != collection:
        return False, (
            f"collection changed from {previous.manifest.get('collection')!r} "
            f"to {collection!r}"
        )
    if previous.stored_embedder != (embedder.name, embedder.dim):
        return False, (
            f"embedder changed from {previous.stored_embedder[0]}:{previous.stored_embedder[1]} "
            f"to {embedder.name}:{embedder.dim}"
        )
    stored_chunking = previous.chunking or {}
    if (
        stored_chunking.get("max_words") != max_words
        or stored_chunking.get("overlap_words") != overlap_words
    ):
        return False, "chunking parameters changed"
    return True, "incremental"


def build_index(
    root: str | Path,
    max_words: int = DEFAULT_MAX_WORDS,
    overlap_words: int = DEFAULT_OVERLAP_WORDS,
    embedder: Embedder | None = None,
    previous: Index | None = None,
    incremental: bool = True,
    collection: str = "default",
) -> Index:
    """Build an index, reusing chunks and vectors from ``previous`` when possible.

    Returns an :class:`~cite.retrieval.Index` whose ``stats`` attribute describes
    the work performed. Pass ``incremental=False`` to force a full rebuild.
    """
    started = time.perf_counter()
    embedder = embedder or get_embedder()
    documents = load_documents(root)

    reuse, reason = _reuse_reason(previous, embedder, max_words, overlap_words, collection)
    if not incremental:
        reuse, reason = False, "full rebuild requested"

    previous_documents: dict[str, dict] = {}
    previous_chunks = {}
    if reuse and previous is not None:
        previous_documents = previous.manifest.get("documents", {})
        previous_chunks = {chunk.chunk_id: chunk for chunk in previous.chunks}

    chunks = []
    vectors: list[list[float]] = []
    manifest_documents: dict[str, dict] = {}
    added = changed = unchanged = 0
    reused_chunks = embedded_chunks = 0
    added_ids: list[str] = []
    changed_ids: list[str] = []

    for document in documents:
        fingerprint = _fingerprint(document)
        record = previous_documents.get(document.doc_id)
        if reuse and record and record.get("sha256") == fingerprint:
            ids = list(record.get("chunks") or [])
            reused = [previous_chunks[chunk_id] for chunk_id in ids if chunk_id in previous_chunks]
            candidate_vectors = (
                [previous.vector_for(chunk.chunk_id) for chunk in reused]
                if previous is not None
                else []
            )
            if len(reused) == len(ids) and all(vector is not None for vector in candidate_vectors):
                chunks.extend(reused)
                vectors.extend(vector for vector in candidate_vectors if vector is not None)
                manifest_documents[document.doc_id] = {
                    "source": document.source,
                    "sha256": fingerprint,
                    "chunks": ids,
                }
                unchanged += 1
                reused_chunks += len(reused)
                continue

        if record is not None and reuse:
            changed += 1
            changed_ids.append(document.doc_id)
        else:
            added += 1
            added_ids.append(document.doc_id)

        fresh = chunk_document(document, max_words, overlap_words)
        fresh_vectors = embedder.embed([chunk.text for chunk in fresh]) if fresh else []
        chunks.extend(fresh)
        vectors.extend(fresh_vectors)
        embedded_chunks += len(fresh)
        manifest_documents[document.doc_id] = {
            "source": document.source,
            "sha256": fingerprint,
            "chunks": [chunk.chunk_id for chunk in fresh],
        }

    removed = 0
    removed_ids: list[str] = []
    if reuse:
        removed_ids = sorted(set(previous_documents) - set(manifest_documents))
        removed = len(removed_ids)
    elif previous is not None:
        removed_ids = sorted(previous.manifest.get("documents", {}))
        removed = len(removed_ids)

    if not reuse:
        # In a full rebuild every document is embedded, not "added" relative to
        # an index we explicitly refused to trust.
        added, changed, unchanged = len(documents), 0, 0
        added_ids = [document.doc_id for document in documents]
        changed_ids = []
        reused_chunks = 0
        embedded_chunks = len(chunks)

    manifest = {
        "version": MANIFEST_VERSION,
        "collection": collection,
        "documents": manifest_documents,
    }
    chunking = {"max_words": max_words, "overlap_words": overlap_words}
    index = Index(
        chunks,
        embedder,
        vectors=vectors,
        manifest=manifest,
        chunking=chunking,
        stored_embedder=(embedder.name, embedder.dim),
    )
    index.stats = BuildStats(
        collection=collection,
        documents=len(documents),
        documents_added=added,
        documents_changed=changed,
        documents_removed=removed,
        documents_unchanged=unchanged,
        chunks=len(chunks),
        chunks_reused=reused_chunks,
        chunks_embedded=embedded_chunks,
        full_rebuild=not reuse,
        reason=reason,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        added_doc_ids=tuple(added_ids),
        changed_doc_ids=tuple(changed_ids),
        removed_doc_ids=tuple(removed_ids),
    )
    return index


def save_index(index: Index, path: str | Path, *, incremental: bool = True) -> Path:
    """Write an index atomically, choosing the store from the file suffix.

    ``.sqlite3`` / ``.sqlite`` / ``.db`` use the per-document SQLite store; every
    other suffix keeps the original JSON format, written through a temp file and
    ``os.replace``.
    """
    destination = Path(path)
    if is_sqlite_path(destination):
        from .store import save_sqlite_index

        return save_sqlite_index(index, destination, incremental=incremental)

    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(index.to_dict(), indent=2) + "\n"
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def load_index(
    path: str | Path,
    embedder: Embedder | None = None,
    require_match: bool = True,
) -> Index:
    """Load an index, defaulting to the embedder recorded in the file.

    ``require_match=False`` is used by the ingester: it needs the previous chunks
    and manifest even when the embedder has changed, so it can decide to rebuild.
    """
    source = Path(path)
    if is_sqlite_path(source):
        from .store import load_sqlite_index

        return load_sqlite_index(source, embedder, require_match=require_match)

    payload = json.loads(source.read_text(encoding="utf-8"))
    stored_name = payload.get("embedder")
    stored_dim = int(payload.get("dim", 0)) or None
    resolved = embedder or get_embedder(stored_name)
    if (
        require_match
        and stored_name
        and (resolved.name != stored_name or (stored_dim and resolved.dim != stored_dim))
    ):
        raise EmbedderMismatchError(
            f"{path} was built with embedder {stored_name}:{stored_dim}, "
            f"but {resolved.name}:{resolved.dim} was requested. Re-ingest with the "
            "same embedder, or pass the matching --embedder."
        )
    return Index.from_dict(payload, resolved)
