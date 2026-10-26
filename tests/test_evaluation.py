from __future__ import annotations

import math

import pytest

from cite.evaluation import (
    doc_ranking,
    load_golden,
    ndcg_at_k,
    percentile,
    recall_at_k,
    reciprocal_rank,
    render_markdown,
    run_config,
)
from cite.models import Chunk, ConfigResult, Scored


def scored(chunk_id: str, doc_id: str, rank: int) -> Scored:
    chunk = Chunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        doc_title=doc_id,
        heading="",
        ordinal=0,
        body="",
        text="",
    )
    return Scored(chunk=chunk, score=1.0 / rank, rank=rank)


def test_doc_ranking_deduplicates_and_preserves_order() -> None:
    hits = [scored("c1", "a", 1), scored("c2", "b", 2), scored("c3", "a", 3)]
    assert doc_ranking(hits) == ["a", "b"]


def test_recall_at_k() -> None:
    assert recall_at_k(["a", "b"], ["a"], 1) == 1.0
    assert recall_at_k(["b", "a"], ["a"], 1) == 0.0
    assert recall_at_k(["b", "a"], ["a", "c"], 2) == 0.5
    assert recall_at_k(["a"], [], 5) == 0.0


def test_reciprocal_rank() -> None:
    assert reciprocal_rank(["a"], ["a"]) == 1.0
    assert reciprocal_rank(["b", "a"], ["a"]) == 0.5
    assert reciprocal_rank(["b", "c"], ["a"]) == 0.0


def test_ndcg_perfect_and_degraded() -> None:
    assert ndcg_at_k(["a"], ["a"], 5) == 1.0
    expected = 1.0 / math.log2(3)
    assert ndcg_at_k(["b", "a"], ["a"], 2) == pytest.approx(expected)
    assert ndcg_at_k(["b"], ["a"], 2) == 0.0


def test_percentile_edges() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 0) == 10.0
    assert percentile(values, 100) == 50.0
    assert percentile([], 50) == 0.0


def test_load_golden_validates_shape(tmp_path) -> None:
    good = tmp_path / "good.yaml"
    good.write_text(
        "k: 3\ncases:\n  - id: a\n    question: why\n    relevant: [doc]\n", encoding="utf-8"
    )
    golden = load_golden(good)
    assert golden.k == 3
    assert golden.cases[0].relevant_docs == ("doc",)

    bad = tmp_path / "bad.yaml"
    bad.write_text("cases:\n  - question: why\n", encoding="utf-8")
    with pytest.raises(ValueError, match="needs 'question' and 'relevant'"):
        load_golden(bad)


def test_render_markdown_contains_every_configuration() -> None:
    results = [
        ConfigResult("BM25 only", 5, 0.5, 0.4, 0.45, 1.0, 2.0),
        ConfigResult("Hybrid (RRF)", 5, 0.6, 0.5, 0.55, 1.5, 3.0),
    ]
    table = render_markdown(results, "hashing")
    assert "| Configuration |" in table
    assert "BM25 only" in table and "Hybrid (RRF)" in table
    assert "embedder: hashing" in table


def test_run_config_scores_perfect_retrieval() -> None:
    from cite.embeddings import HashingEmbedder
    from cite.models import EvalCase
    from cite.retrieval import Index

    chunk = Chunk(
        chunk_id="c1",
        doc_id="target",
        doc_title="Target",
        heading="",
        ordinal=0,
        body="usage based overage pricing per thousand events",
        text="target usage based overage pricing per thousand events",
    )
    index = Index([chunk], HashingEmbedder(dim=64))
    case = EvalCase(case_id="q", question="overage pricing", relevant_docs=("target",))
    result = run_config(index, [case], "bm25", "bm25", False, k=1)
    assert result.recall_at_k == 1.0
    assert result.mrr == 1.0
    assert result.ndcg_at_k == 1.0
    assert len(result.cases) == 1
