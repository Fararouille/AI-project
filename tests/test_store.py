from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cite.embeddings import HashingEmbedder
from cite.ingest import EmbedderMismatchError, build_index, load_index, save_index
from cite.store import IndexFormatError, SQLiteIndexStore, is_sqlite_path


def make_corpus(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "alpha.md").write_text(
        "# Alpha\n\nAlpha rate limit is 10 requests per minute.", encoding="utf-8"
    )
    (root / "beta.md").write_text(
        "# Beta\n\nBeta retention keeps events for 30 days.", encoding="utf-8"
    )
    return root


def chunk_rowids(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as connection:
        return {
            chunk_id: rowid
            for chunk_id, rowid in connection.execute(
                "SELECT chunk_id, rowid FROM chunks"
            ).fetchall()
        }


def test_sqlite_roundtrip_preserves_chunks_vectors_and_manifest(tmp_path) -> None:
    embedder = HashingEmbedder(dim=64)
    index = build_index(make_corpus(tmp_path / "corpus"), embedder=embedder, collection="docs")
    path = save_index(index, tmp_path / "index.sqlite3")

    restored = load_index(path, embedder)
    assert len(restored) == len(index)
    assert restored.manifest["collection"] == "docs"
    assert set(restored.manifest["documents"]) == {"alpha", "beta"}
    assert restored.stored_embedder == ("hashing", 64)
    for chunk in index.chunks:
        assert restored.vector_for(chunk.chunk_id) == pytest.approx(
            index.vector_for(chunk.chunk_id), abs=1e-5
        )
    assert restored.search("retention", k=1, mode="dense")[0].chunk.doc_id == "beta"


def test_sqlite_incremental_save_leaves_unchanged_rows_untouched(tmp_path) -> None:
    corpus = make_corpus(tmp_path / "corpus")
    embedder = HashingEmbedder(dim=64)
    path = tmp_path / "index.sqlite3"
    first = build_index(corpus, embedder=embedder)
    save_index(first, path)
    before = chunk_rowids(path)

    (corpus / "alpha.md").write_text(
        "# Alpha\n\nAlpha rate limit is 20 requests per minute.", encoding="utf-8"
    )
    second = build_index(corpus, embedder=embedder, previous=load_index(path, embedder))
    assert second.stats is not None
    assert second.stats.changed_doc_ids == ("alpha",)
    save_index(second, path)

    after = chunk_rowids(path)
    beta_before = {key: value for key, value in before.items() if key.startswith("beta-")}
    beta_after = {key: value for key, value in after.items() if key.startswith("beta-")}
    assert beta_before == beta_after
    assert second.store_stats.mode == "incremental"
    assert second.store_stats.chunks_written > 0


def test_sqlite_save_removes_deleted_document_rows(tmp_path) -> None:
    corpus = make_corpus(tmp_path / "corpus")
    embedder = HashingEmbedder(dim=64)
    path = tmp_path / "index.sqlite3"
    save_index(build_index(corpus, embedder=embedder), path)
    (corpus / "beta.md").unlink()

    second = build_index(corpus, embedder=embedder, previous=load_index(path, embedder))
    save_index(second, path)
    with sqlite3.connect(path) as connection:
        documents = [
            row[0] for row in connection.execute("SELECT doc_id FROM documents ORDER BY doc_id")
        ]
        orphaned = connection.execute(
            "SELECT COUNT(*) FROM chunks WHERE doc_id = 'beta'"
        ).fetchone()[0]
    assert documents == ["alpha"]
    assert orphaned == 0
    assert {chunk.doc_id for chunk in load_index(path, embedder).chunks} == {"alpha"}


def test_sqlite_rebuild_with_embedder_mismatch_is_a_full_write(tmp_path) -> None:
    corpus = make_corpus(tmp_path / "corpus")
    path = tmp_path / "index.sqlite3"
    save_index(build_index(corpus, embedder=HashingEmbedder(dim=64)), path)

    with pytest.raises(EmbedderMismatchError, match="built with embedder"):
        load_index(path, HashingEmbedder(dim=128))

    previous = load_index(path, HashingEmbedder(dim=128), require_match=False)
    rebuilt = build_index(
        corpus, embedder=HashingEmbedder(dim=128), previous=previous
    )
    assert rebuilt.stats is not None and rebuilt.stats.full_rebuild is True
    save_index(rebuilt, path)
    assert load_index(path, HashingEmbedder(dim=128)).embedder.dim == 128


def test_json_store_still_works_and_dispatch_is_by_suffix(tmp_path) -> None:
    index = build_index(make_corpus(tmp_path / "corpus"), embedder=HashingEmbedder(dim=64))
    json_path = save_index(index, tmp_path / "index.json")
    sqlite_path = save_index(index, tmp_path / "index.sqlite3")

    assert json.loads(json_path.read_text(encoding="utf-8"))["version"] == 2
    assert sqlite_path.read_bytes().startswith(b"SQLite format 3")
    assert is_sqlite_path(sqlite_path) and not is_sqlite_path(json_path)
    assert len(load_index(json_path, HashingEmbedder(dim=64))) == len(index)
    assert len(load_index(sqlite_path, HashingEmbedder(dim=64))) == len(index)


def test_sqlite_store_rejects_a_json_file_with_a_sqlite_suffix(tmp_path) -> None:
    path = tmp_path / "index.sqlite3"
    path.write_text('{"version": 2}', encoding="utf-8")
    with pytest.raises(IndexFormatError, match="not a SQLite index|no cite index"):
        SQLiteIndexStore(path).load()


def test_out_of_band_row_deletion_falls_back_to_a_full_write(tmp_path) -> None:
    corpus = make_corpus(tmp_path / "corpus")
    embedder = HashingEmbedder(dim=64)
    path = tmp_path / "index.sqlite3"
    first = build_index(corpus, embedder=embedder)
    save_index(first, path)

    second = build_index(corpus, embedder=embedder, previous=load_index(path, embedder))
    # Delete a row belonging to an unchanged document after the rebuild. The save
    # has no reason to touch beta, so only a count check can notice the damage.
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM chunks WHERE chunk_id = (SELECT chunk_id FROM chunks "
            "WHERE doc_id = 'beta' LIMIT 1)"
        )
    save_index(second, path)
    assert second.store_stats.mode == "full"
    assert {chunk.doc_id for chunk in load_index(path, embedder).chunks} == {"alpha", "beta"}


def test_schema_version_mismatch_is_explicit(tmp_path) -> None:
    path = tmp_path / "index.sqlite3"
    SQLiteIndexStore(path).save(
        build_index(
            make_corpus(tmp_path / "corpus"), embedder=HashingEmbedder(dim=64)
        )
    )
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE meta SET value = '999' WHERE key = 'version'")
    with pytest.raises(IndexFormatError, match="schema"):
        load_index(path)


def test_legacy_json_is_migrated_to_the_configured_sqlite_path(tmp_path, monkeypatch) -> None:
    from cite.api import _migrate_legacy_json
    from cite.config import Settings

    corpus = make_corpus(tmp_path / "corpus")
    embedder = HashingEmbedder()
    legacy = build_index(corpus, embedder=embedder, collection="default")
    save_index(legacy, tmp_path / "index.json")

    settings = Settings(
        corpus_dir=corpus,
        index_path=tmp_path / "index.sqlite3",
        golden_path=tmp_path / "golden.yaml",
    )
    monkeypatch.delenv("CITE_EMBEDDER", raising=False)
    migrated = _migrate_legacy_json(settings)

    assert migrated == settings.index_path
    restored = load_index(settings.index_path, embedder)
    assert len(restored) == len(legacy)
    assert set(restored.manifest["documents"]) == {"alpha", "beta"}
