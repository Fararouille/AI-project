from __future__ import annotations

import pytest

from cite.embeddings import HashingEmbedder
from cite.models import Chunk
from cite.retrieval import BM25Index, DenseIndex, Index, reciprocal_rank_fusion


def make_chunk(chunk_id: str, body: str, doc_id: str | None = None) -> Chunk:
    doc = doc_id or chunk_id
    return Chunk(
        chunk_id=chunk_id,
        doc_id=doc,
        doc_title=doc,
        heading=doc,
        ordinal=0,
        body=body,
        text=f"{doc}\n{body}",
    )


CHUNKS = [
    make_chunk("c1", "Requests are limited to 600 per minute for ingestion endpoints."),
    make_chunk("c2", "Dashboards cache tile results for five minutes before refreshing."),
    make_chunk("c3", "Webhook deliveries retry with exponential backoff when they fail."),
    make_chunk("c4", "Rate limit headers include remaining requests and a reset timestamp."),
]


def test_bm25_ranks_documents_containing_all_query_terms_first() -> None:
    index = BM25Index(CHUNKS)
    hits = index.search("rate limit headers", k=4)
    assert hits[0].chunk.chunk_id == "c4"
    assert hits[0].rank == 1


def test_bm25_returns_nothing_for_unseen_terms() -> None:
    index = BM25Index(CHUNKS)
    assert index.search("kubernetes helm chart") == []


def test_bm25_ignores_stopwords_only_queries() -> None:
    index = BM25Index(CHUNKS)
    assert index.search("what is the") == []


def test_idf_prefers_rare_terms() -> None:
    index = BM25Index(CHUNKS)
    assert index.idf("webhook") > index.idf("requests")


def test_reciprocal_rank_fusion_rewards_agreement() -> None:
    a = BM25Index(CHUNKS).search("rate limit", k=4)
    b = DenseIndex(CHUNKS, HashingEmbedder(dim=128)).search("rate limit", k=4)
    fused = reciprocal_rank_fusion([a, b])
    ids = [hit.chunk.chunk_id for hit in fused]
    # Dense retrieval scores every chunk, so the fused list is wider than BM25's.
    # What matters is that agreement wins: the two chunks both retrievers like
    # must occupy the top two positions.
    assert set(ids) == {"c1", "c2", "c3", "c4"}
    assert set(ids[:2]) == {"c1", "c4"}


def test_dense_retriever_finds_exact_phrase() -> None:
    index = DenseIndex(CHUNKS, HashingEmbedder(dim=256))
    hits = index.search("retry with exponential backoff", k=2)
    assert hits[0].chunk.chunk_id == "c3"


def test_auto_mode_falls_back_to_lexical_for_lexical_embedders() -> None:
    index = Index(CHUNKS, HashingEmbedder(dim=64))
    assert index.resolve_mode("auto") == "bm25"
    assert index.resolve_mode("hybrid") == "hybrid"


def test_index_supports_every_mode() -> None:
    index = Index(CHUNKS, HashingEmbedder(dim=128))
    for mode in ("bm25", "dense", "hybrid"):
        hits = index.search("rate limit", k=2, mode=mode)
        assert hits, f"{mode} returned nothing"
        assert [hit.rank for hit in hits] == list(range(1, len(hits) + 1))


def test_index_rejects_unknown_mode() -> None:
    index = Index(CHUNKS, HashingEmbedder(dim=64))
    with pytest.raises(ValueError, match="unknown retrieval mode"):
        index.search("anything", mode="magic")


def test_reranker_promotes_heading_matches() -> None:
    index = Index(CHUNKS, HashingEmbedder(dim=128))
    plain = index.search("webhook retry", k=3, mode="hybrid", rerank=False)
    reranked = index.search("webhook retry", k=3, mode="hybrid", rerank=True)
    assert reranked[0].chunk.chunk_id == "c3"
    assert len(reranked) <= len(plain)


def test_index_round_trips_through_dict() -> None:
    index = Index(CHUNKS, HashingEmbedder(dim=64))
    restored = Index.from_dict(index.to_dict(), HashingEmbedder(dim=64))
    assert [c.chunk_id for c in restored.chunks] == [c.chunk_id for c in CHUNKS]
    assert restored.search("dashboard cache", k=1)[0].chunk.chunk_id == "c2"


def test_empty_index_is_safe() -> None:
    index = Index([], HashingEmbedder(dim=64))
    assert index.search("anything", k=5) == []
    assert len(index) == 0
