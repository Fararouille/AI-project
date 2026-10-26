from __future__ import annotations

from dataclasses import replace

import pytest

from cite.embeddings import HashingEmbedder
from cite.evaluation import (
    _split_cited_sentences,
    load_golden,
    render_answer_markdown,
    run_answer_eval,
    score_answer,
)
from cite.models import Answer, Chunk, Citation, EvalCase, Scored
from cite.providers import MockProvider
from cite.retrieval import Index


def make_answer(text: str, body: str, doc_id: str = "pricing") -> Answer:
    chunk = Chunk(
        chunk_id="c1",
        doc_id=doc_id,
        doc_title=doc_id.title(),
        heading="Rates",
        ordinal=0,
        body=body,
        text=f"{doc_id}\nRates\n{body}",
    )
    return Answer(
        question="q",
        text=text,
        citations=[
            Citation(
                marker=1,
                chunk_id=chunk.chunk_id,
                doc_id=chunk.doc_id,
                doc_title=chunk.doc_title,
                heading=chunk.heading,
                snippet=body,
                score=1.0,
            )
        ],
        retrieval_mode="bm25",
        reranked=False,
        provider="mock",
        latency_ms=1.0,
        retrieved=[Scored(chunk=chunk, score=1.0, rank=1)],
    )


def case(**overrides) -> EvalCase:
    values = {
        "case_id": "q",
        "question": "how much",
        "relevant_docs": ("pricing",),
        "expects": ("$5",),
    }
    values.update(overrides)
    return EvalCase(**values)


def test_split_cited_sentences_attaches_trailing_markers() -> None:
    sentences = _split_cited_sentences("First claim. [1] Second claim. [2]")
    assert sentences == ["First claim. [1]", "Second claim. [2]"]


def test_split_cited_sentences_handles_markers_before_punctuation() -> None:
    assert _split_cited_sentences("One [1]. Two [2].") == ["One [1].", "Two [2]."]


def test_verbatim_cited_claim_is_supported() -> None:
    answer = make_answer(
        "Widgets cost $5 per month. [1]", "Widgets cost $5 per month."
    )
    score = score_answer(answer, case())
    assert score.answered is True
    assert score.grounded_rate == 1.0
    assert score.support_rate == 1.0
    assert score.unsupported_claim_rate == 0.0
    assert score.citation_precision == 1.0
    assert score.citation_recall == 1.0
    assert score.fact_recall == 1.0


def test_miscited_claim_is_unsupported() -> None:
    answer = make_answer(
        "Widgets cost $5 per month. [1]", "Widgets are blue and have three corners."
    )
    score = score_answer(answer, case())
    assert score.grounded_rate == 1.0
    assert score.support_rate == 0.0
    assert score.unsupported_claim_rate == 1.0


def test_uncited_claim_counts_as_unsupported() -> None:
    answer = make_answer("Widgets cost $5 per month.", "Widgets cost $5 per month.")
    answer.citations = []
    score = score_answer(answer, case())
    assert score.grounded_rate == 0.0
    assert score.support_rate == 0.0
    assert score.unsupported_claim_rate == 1.0


def test_refusal_does_not_count_as_unsupported_claim() -> None:
    answer = make_answer(
        "I could not find that in the provided sources.",
        "Widgets cost $5 per month.",
    )
    answer.citations = []
    score = score_answer(answer, case())
    assert score.answered is False
    assert score.unsupported_claim_rate == 0.0
    assert score.fact_recall == 0.0


def test_citation_precision_uses_relevant_documents() -> None:
    answer = make_answer("Widgets cost $5. [1]", "Widgets cost $5 per month.")
    answer.citations[0] = replace(answer.citations[0], doc_id="other")
    score = score_answer(answer, case(relevant_docs=("pricing",)))
    assert score.citation_precision == 0.0
    assert score.citation_recall == 0.0


def test_fact_recall_requires_every_expected_fact() -> None:
    answer = make_answer("Widgets cost $5 per month. [1]", "Widgets cost $5 per month.")
    assert score_answer(answer, case(expects=("$5", "per month"))).fact_recall == 1.0
    assert score_answer(answer, case(expects=("$5", "per year"))).fact_recall == 0.5


def test_load_golden_parses_expects(tmp_path) -> None:
    path = tmp_path / "golden.yaml"
    path.write_text(
        "k: 3\ncases:\n  - id: a\n    question: why\n    relevant: [doc]\n"
        "    expects: ['$5', 'per month']\n",
        encoding="utf-8",
    )
    assert load_golden(path).cases[0].expects == ("$5", "per month")


def test_run_answer_eval_aggregates_and_renders() -> None:
    chunk = Chunk(
        chunk_id="c1",
        doc_id="pricing",
        doc_title="Pricing",
        heading="Rates",
        ordinal=0,
        body="Widgets cost $5 per month.",
        text="Pricing\nRates\nWidgets cost $5 per month.",
    )
    index = Index([chunk], HashingEmbedder(dim=64))
    result = run_answer_eval(
        index,
        [case(question="How much do widgets cost?", relevant_docs=("pricing",))],
        provider=MockProvider(),
        k=1,
    )
    assert result.answered_rate == 1.0
    assert result.fact_cases == 1
    assert result.fact_recall == 1.0
    assert result.cases[0].grounded_rate == 1.0

    table = render_answer_markdown([result])
    assert "Expected facts" in table
    assert "mock provider" in table
    assert "Lexical support is a proxy" in table


def test_missing_retrieved_context_scores_zero_support() -> None:
    answer = make_answer("Widgets cost $5. [1]", "Widgets cost $5.")
    answer.retrieved = []
    score = score_answer(answer, case())
    assert score.support_rate == 0.0


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_answer_is_not_answered(text: str) -> None:
    answer = make_answer(text, "Widgets cost $5.")
    assert score_answer(answer, case()).answered is False
