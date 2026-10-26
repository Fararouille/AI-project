from __future__ import annotations

import json

import pytest

from cite.embeddings import HashingEmbedder
from cite.ingest import (
    EmbedderMismatchError,
    build_index,
    load_index,
    save_index,
)


def make_corpus(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "alpha.md").write_text(
        "# Alpha\n\nAlpha rate limit is 10 requests per minute.", encoding="utf-8"
    )
    (root / "beta.md").write_text(
        "# Beta\n\nBeta retention keeps events for 30 days.", encoding="utf-8"
    )
    return root


def chunks_for(index, doc_id):
    return [chunk for chunk in index.chunks if chunk.doc_id == doc_id]


def test_first_build_embeds_everything(tmp_path) -> None:
    corpus = make_corpus(tmp_path / "corpus")
    index = build_index(corpus, embedder=HashingEmbedder(dim=64))
    assert index.stats is not None
    assert index.stats.full_rebuild is True
    assert index.stats.documents_added == 2
    assert index.stats.chunks_embedded == len(index)
    assert index.stats.chunks_reused == 0


def test_unchanged_rebuild_reuses_chunks_and_vectors(tmp_path) -> None:
    corpus = make_corpus(tmp_path / "corpus")
    embedder = HashingEmbedder(dim=64)
    first = build_index(corpus, embedder=embedder)
    path = save_index(first, tmp_path / "index.json")

    previous = load_index(path, embedder)
    second = build_index(corpus, embedder=embedder, previous=previous)

    assert second.stats is not None
    assert second.stats.full_rebuild is False
    assert second.stats.documents_unchanged == 2
    assert second.stats.chunks_embedded == 0
    assert second.stats.chunks_reused == len(second)
    assert second.stats.chunks == len(first)


def test_one_changed_document_only_reembeds_its_chunks(tmp_path) -> None:
    corpus = make_corpus(tmp_path / "corpus")
    embedder = HashingEmbedder(dim=64)
    first = build_index(corpus, embedder=embedder)
    path = save_index(first, tmp_path / "index.json")
    (corpus / "alpha.md").write_text(
        "# Alpha\n\nAlpha rate limit is 10 requests per minute.\n\nA new paragraph.",
        encoding="utf-8",
    )

    previous = load_index(path, embedder)
    second = build_index(corpus, embedder=embedder, previous=previous)

    alpha_chunks = len(chunks_for(second, "alpha"))
    assert second.stats is not None
    assert second.stats.documents_changed == 1
    assert second.stats.documents_unchanged == 1
    assert second.stats.chunks_embedded == alpha_chunks
    assert second.stats.chunks_reused == len(second) - alpha_chunks
    assert second.manifest["documents"]["alpha"]["sha256"] != first.manifest["documents"][
        "alpha"
    ]["sha256"]


def test_deleted_document_is_removed(tmp_path) -> None:
    corpus = make_corpus(tmp_path / "corpus")
    embedder = HashingEmbedder(dim=64)
    first = build_index(corpus, embedder=embedder)
    path = save_index(first, tmp_path / "index.json")
    (corpus / "beta.md").unlink()

    second = build_index(corpus, embedder=embedder, previous=load_index(path, embedder))
    assert second.stats is not None
    assert second.stats.documents_removed == 1
    assert {chunk.doc_id for chunk in second.chunks} == {"alpha"}
    assert "beta" not in second.manifest["documents"]


def test_embedder_mismatch_forces_full_rebuild(tmp_path) -> None:
    corpus = make_corpus(tmp_path / "corpus")
    first = build_index(corpus, embedder=HashingEmbedder(dim=64))
    path = save_index(first, tmp_path / "index.json")

    with pytest.raises(EmbedderMismatchError, match="built with embedder"):
        load_index(path, HashingEmbedder(dim=128))

    previous = load_index(path, HashingEmbedder(dim=128), require_match=False)
    rebuilt = build_index(
        corpus, embedder=HashingEmbedder(dim=128), previous=previous
    )
    assert rebuilt.stats is not None
    assert rebuilt.stats.full_rebuild is True
    assert "embedder changed" in rebuilt.stats.reason
    assert rebuilt.stats.chunks_embedded == len(rebuilt)


def test_chunking_change_forces_full_rebuild(tmp_path) -> None:
    corpus = make_corpus(tmp_path / "corpus")
    embedder = HashingEmbedder(dim=64)
    first = build_index(corpus, embedder=embedder)
    path = save_index(first, tmp_path / "index.json")

    rebuilt = build_index(
        corpus,
        max_words=20,
        overlap_words=5,
        embedder=embedder,
        previous=load_index(path, embedder),
    )
    assert rebuilt.stats is not None
    assert rebuilt.stats.full_rebuild is True
    assert "chunking parameters changed" in rebuilt.stats.reason


def test_incremental_false_forces_full_rebuild(tmp_path) -> None:
    corpus = make_corpus(tmp_path / "corpus")
    embedder = HashingEmbedder(dim=64)
    first = build_index(corpus, embedder=embedder)
    path = save_index(first, tmp_path / "index.json")
    rebuilt = build_index(
        corpus,
        embedder=embedder,
        previous=load_index(path, embedder),
        incremental=False,
    )
    assert rebuilt.stats is not None
    assert rebuilt.stats.full_rebuild is True
    assert rebuilt.stats.reason == "full rebuild requested"


def test_vectors_survive_save_and_load(tmp_path) -> None:
    corpus = make_corpus(tmp_path / "corpus")
    embedder = HashingEmbedder(dim=64)
    first = build_index(corpus, embedder=embedder)
    path = save_index(first, tmp_path / "index.json")
    restored = load_index(path, embedder)

    for chunk in first.chunks:
        assert restored.vector_for(chunk.chunk_id) == pytest.approx(
            first.vector_for(chunk.chunk_id), abs=1e-6
        )
    assert restored.search("retention", k=1, mode="dense")[0].chunk.doc_id == "beta"


def test_corrupt_vector_block_is_recomputed(tmp_path) -> None:
    corpus = make_corpus(tmp_path / "corpus")
    embedder = HashingEmbedder(dim=64)
    index = build_index(corpus, embedder=embedder)
    payload = index.to_dict()
    payload["vectors"] = [[0.0]]
    path = tmp_path / "index.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = load_index(path, embedder)
    assert len(restored.dense.vectors) == len(restored)
    assert restored.search("rate limit", k=1, mode="dense")


def test_manifest_records_collection_and_hashes(tmp_path) -> None:
    corpus = make_corpus(tmp_path / "corpus")
    index = build_index(
        corpus, embedder=HashingEmbedder(dim=64), collection="support"
    )
    assert index.manifest["collection"] == "support"
    assert set(index.manifest["documents"]) == {"alpha", "beta"}
    for record in index.manifest["documents"].values():
        assert len(record["sha256"]) == 64
        assert record["chunks"]


def test_save_index_is_atomic_and_leaves_no_temp_file(tmp_path) -> None:
    corpus = make_corpus(tmp_path / "corpus")
    index = build_index(corpus, embedder=HashingEmbedder(dim=64))
    destination = tmp_path / "nested" / "index.json"
    save_index(index, destination)
    assert destination.exists()
    assert list(destination.parent.glob("*.tmp")) == []
    assert json.loads(destination.read_text(encoding="utf-8"))["version"] == 2
