"""SQLite index storage with per-document updates.

The JSON index is transparent and diffable, which is why it was the first store.
It is also rewritten in full on every save: one edited paragraph rewrites every
chunk and every vector. At a few hundred chunks that is invisible; at tens of
thousands it is the difference between a fast deploy and a stalled request.

This module adds a second, opt-in store with the same :class:`~cite.retrieval.Index`
contract:

* one row per document and one row per chunk, so a save touches only documents
  whose SHA-256 changed plus the documents that disappeared
* vectors as float32 blobs, roughly half the size of JSON's decimal text
* the whole save inside one SQLite transaction, so a crash cannot leave a
  half-written index
* a metadata table holding the embedder, dimension, collection, and chunking
  parameters needed to decide whether reuse is legal

The JSON path stays supported for small corpora, human inspection, and existing
deployments. :func:`cite.ingest.save_index` dispatches on the file suffix.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path

from .embeddings import Embedder, get_embedder
from .models import Chunk
from .retrieval import Index

SCHEMA_VERSION = 1
SQLITE_SUFFIXES = frozenset({".sqlite3", ".sqlite", ".db"})
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    position INTEGER NOT NULL,
    source TEXT NOT NULL,
    sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    doc_title TEXT NOT NULL,
    heading TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    body TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL,
    vector BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
"""


class IndexFormatError(ValueError):
    """The file is not a cite SQLite index, or uses an incompatible schema."""


@dataclass(frozen=True, slots=True)
class StoreSaveStats:
    """What a store write actually touched, useful in tests and benchmarks."""

    mode: str
    documents_written: int
    documents_deleted: int
    chunks_written: int
    chunks_deleted: int


def is_sqlite_path(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SQLITE_SUFFIXES


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes, dim: int) -> list[float] | None:
    if len(blob) != dim * 4:
        return None
    return list(struct.unpack(f"<{dim}f", blob))


def _manifest_documents(index: Index) -> dict[str, dict]:
    documents = index.manifest.get("documents")
    if documents:
        return {str(doc_id): dict(record) for doc_id, record in documents.items()}

    # A manually constructed Index has no manifest. Derive enough of one that a
    # later build_index treats every document as unknown and re-embeds it, rather
    # than trusting a hash that was never computed.
    derived: dict[str, dict] = {}
    for chunk in index.chunks:
        record = derived.setdefault(
            chunk.doc_id,
            {"source": chunk.source, "sha256": "", "chunks": []},
        )
        record["chunks"].append(chunk.chunk_id)
    return derived


def _chunk_rows(index: Index) -> list[tuple]:
    index.ensure_vectors()
    rows = []
    for chunk, vector in zip(index.chunks, index.dense.vectors, strict=True):
        rows.append(
            (
                chunk.chunk_id,
                chunk.doc_id,
                chunk.doc_title,
                chunk.heading,
                chunk.ordinal,
                chunk.body,
                chunk.text,
                chunk.source,
                _pack(vector),
            )
        )
    return rows


class SQLiteIndexStore:
    """Read and write an :class:`Index` in a single SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # -- connection --------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path), timeout=30.0, isolation_level=None
        )
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _meta(connection: sqlite3.Connection) -> dict[str, str]:
        rows = connection.execute("SELECT key, value FROM meta").fetchall()
        return {str(key): str(value) for key, value in rows}

    # -- save --------------------------------------------------------------
    def save(self, index: Index, *, incremental: bool = True) -> StoreSaveStats:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        documents = _manifest_documents(index)
        stats = index.stats
        can_patch = (
            incremental
            and stats is not None
            and not stats.full_rebuild
            and self.path.exists()
        )

        try:
            connection = self._connect()
        except sqlite3.DatabaseError as exc:
            raise IndexFormatError(
                f"{self.path} is not a SQLite index; use the matching suffix or re-ingest"
            ) from exc
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            meta = self._meta(connection)
            if meta and meta.get("version") != str(SCHEMA_VERSION):
                raise IndexFormatError(
                    f"{self.path} uses index schema {meta.get('version')!r}; "
                    f"this build writes {SCHEMA_VERSION}. Re-ingest to migrate."
                )

            compatible = can_patch and meta and self._compatible(meta, index)
            if compatible:
                try:
                    result = self._patch(connection, index, documents)
                    self._write_meta(connection, index)
                    written_docs = connection.execute(
                        "SELECT COUNT(*) FROM documents"
                    ).fetchone()[0]
                    written_chunks = connection.execute(
                        "SELECT COUNT(*) FROM chunks"
                    ).fetchone()[0]
                    if written_docs != len(documents) or written_chunks != len(index):
                        raise _NeedsFullWrite
                    connection.execute("COMMIT")
                    index.store_stats = result
                    return result
                except _NeedsFullWrite:
                    # The file exists but no longer matches the index it claims to
                    # describe (for example a chunk row was deleted out of band).
                    connection.execute("ROLLBACK")
                    connection.execute("BEGIN IMMEDIATE")

            result = self._replace_all(connection, index, documents)
            connection.execute("COMMIT")
            index.store_stats = result
            return result
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - transaction already gone
                pass
            raise
        finally:
            connection.close()

    @staticmethod
    def _compatible(meta: dict[str, str], index: Index) -> bool:
        if meta.get("embedder") != index.embedder.name:
            return False
        if int(meta.get("dim", "0") or 0) != index.embedder.dim:
            return False
        if meta.get("collection") != (index.manifest.get("collection") or "default"):
            return False
        stored_chunking = json.loads(meta.get("chunking") or "{}")
        return stored_chunking == (index.chunking or {})

    def _replace_all(
        self, connection: sqlite3.Connection, index: Index, documents: dict[str, dict]
    ) -> StoreSaveStats:
        previous_documents = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        previous_chunks = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        connection.execute("DELETE FROM chunks")
        connection.execute("DELETE FROM documents")
        self._write_documents(connection, documents)
        rows = _chunk_rows(index)
        connection.executemany(
            """
            INSERT INTO chunks (
                chunk_id, doc_id, doc_title, heading, ordinal, body, text, source, vector
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self._write_meta(connection, index)
        return StoreSaveStats(
            mode="full",
            documents_written=len(documents),
            documents_deleted=previous_documents,
            chunks_written=len(rows),
            chunks_deleted=previous_chunks,
        )

    def _patch(
        self, connection: sqlite3.Connection, index: Index, documents: dict[str, dict]
    ) -> StoreSaveStats:
        stats = index.stats
        assert stats is not None
        removed = tuple(stats.removed_doc_ids)
        dirty = tuple(dict.fromkeys((*stats.added_doc_ids, *stats.changed_doc_ids)))

        deleted_chunks = 0
        for doc_id in removed:
            deleted_chunks += connection.execute(
                "DELETE FROM chunks WHERE doc_id = ?", (doc_id,)
            ).rowcount
            connection.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))

        written_chunks = 0
        all_rows = _chunk_rows(index)
        for doc_id in dirty:
            deleted_chunks += connection.execute(
                "DELETE FROM chunks WHERE doc_id = ?", (doc_id,)
            ).rowcount
            rows = [row for row in all_rows if row[1] == doc_id]
            connection.executemany(
                """
                INSERT INTO chunks (
                    chunk_id, doc_id, doc_title, heading, ordinal, body, text, source, vector
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            written_chunks += len(rows)

        # Document rows are cheap; rewriting them keeps positions aligned with the
        # manifest without touching any unchanged chunk or vector.
        self._write_documents(connection, documents)
        return StoreSaveStats(
            mode="incremental",
            documents_written=len(dirty),
            documents_deleted=len(removed),
            chunks_written=written_chunks,
            chunks_deleted=deleted_chunks,
        )

    @staticmethod
    def _write_documents(
        connection: sqlite3.Connection, documents: dict[str, dict]
    ) -> None:
        connection.executemany(
            """
            INSERT INTO documents (doc_id, position, source, sha256)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                position = excluded.position,
                source = excluded.source,
                sha256 = excluded.sha256
            """,
            [
                (doc_id, position, str(record.get("source", "")), str(record.get("sha256", "")))
                for position, (doc_id, record) in enumerate(documents.items())
            ],
        )

    @staticmethod
    def _write_meta(connection: sqlite3.Connection, index: Index) -> None:
        values = {
            "version": str(SCHEMA_VERSION),
            "embedder": index.embedder.name,
            "dim": str(index.embedder.dim),
            "collection": index.manifest.get("collection") or "default",
            "chunking": json.dumps(index.chunking or {}, sort_keys=True),
            "manifest_version": str(index.manifest.get("version", 1)),
        }
        connection.executemany(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            list(values.items()),
        )

    # -- load --------------------------------------------------------------
    def load(
        self, embedder: Embedder | None = None, require_match: bool = True
    ) -> Index:
        if not self.path.exists():
            raise FileNotFoundError(f"index not found: {self.path}")
        try:
            connection = self._connect()
        except sqlite3.DatabaseError as exc:
            raise IndexFormatError(
                f"{self.path} is not a SQLite index; use the matching suffix or re-ingest"
            ) from exc
        try:
            try:
                meta = self._meta(connection)
            except sqlite3.DatabaseError as exc:
                raise IndexFormatError(
                    f"{self.path} is not a SQLite index; use the matching suffix or re-ingest"
                ) from exc
            if not meta:
                raise IndexFormatError(f"{self.path} has no cite index metadata")
            if meta.get("version") != str(SCHEMA_VERSION):
                raise IndexFormatError(
                    f"{self.path} uses index schema {meta.get('version')!r}; "
                    f"this build reads {SCHEMA_VERSION}. Re-ingest to migrate."
                )

            stored_name = meta.get("embedder", "")
            stored_dim = int(meta.get("dim", "0") or 0)
            resolved = embedder or get_embedder(stored_name or None)
            if (
                require_match
                and stored_name
                and (
                    resolved.name != stored_name
                    or (stored_dim and resolved.dim != stored_dim)
                )
            ):
                from .ingest import EmbedderMismatchError

                raise EmbedderMismatchError(
                    f"{self.path} was built with embedder {stored_name}:{stored_dim}, "
                    f"but {resolved.name}:{resolved.dim} was requested. Re-ingest with "
                    "the same embedder, or pass the matching --embedder."
                )

            rows = connection.execute(
                """
                SELECT c.chunk_id, c.doc_id, c.doc_title, c.heading, c.ordinal,
                       c.body, c.text, c.source, c.vector
                FROM chunks c
                JOIN documents d ON d.doc_id = c.doc_id
                ORDER BY d.position, c.ordinal, c.chunk_id
                """
            ).fetchall()
            documents = connection.execute(
                "SELECT doc_id, source, sha256 FROM documents ORDER BY position"
            ).fetchall()
        finally:
            connection.close()

        chunks = [
            Chunk(
                chunk_id=row[0],
                doc_id=row[1],
                doc_title=row[2],
                heading=row[3],
                ordinal=row[4],
                body=row[5],
                text=row[6],
                source=row[7],
            )
            for row in rows
        ]

        vectors: list[list[float]] | None = []
        for row in rows:
            if row[8] is None:
                vectors = None
                break
            vector = _unpack(row[8], resolved.dim)
            if vector is None:
                vectors = None
                break
            vectors.append(vector)
        if not rows:
            vectors = []

        manifest_documents: dict[str, dict] = {}
        chunk_ids: dict[str, list[str]] = {}
        for chunk in chunks:
            chunk_ids.setdefault(chunk.doc_id, []).append(chunk.chunk_id)
        for doc_id, source, sha256 in documents:
            manifest_documents[str(doc_id)] = {
                "source": str(source),
                "sha256": str(sha256),
                "chunks": chunk_ids.get(str(doc_id), []),
            }

        manifest = {
            "version": int(meta.get("manifest_version", "1") or 1),
            "collection": meta.get("collection") or "default",
            "documents": manifest_documents,
        }
        chunking = json.loads(meta.get("chunking") or "{}")
        return Index(
            chunks,
            resolved,
            vectors=vectors,
            manifest=manifest,
            chunking=chunking,
            stored_embedder=(stored_name, stored_dim) if stored_name else None,
        )


class _NeedsFullWrite(Exception):
    pass


def load_sqlite_index(
    path: str | Path,
    embedder: Embedder | None = None,
    require_match: bool = True,
) -> Index:
    return SQLiteIndexStore(path).load(embedder, require_match=require_match)


def save_sqlite_index(index: Index, path: str | Path, *, incremental: bool = True) -> Path:
    SQLiteIndexStore(path).save(index, incremental=incremental)
    return Path(path)
