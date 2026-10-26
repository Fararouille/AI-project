from __future__ import annotations

from cite.answer import answer_question, build_citations
from cite.embeddings import HashingEmbedder
from cite.models import Chunk, Scored
from cite.providers import estimate_tokens
from cite.retrieval import Index


def build_index() -> Index:
    chunks = [
        Chunk(
            chunk_id="c1",
            doc_id="rate-limits",
            doc_title="Rate Limits",
            heading="Limits",
            ordinal=0,
            body="Event ingestion is limited to 600 requests per minute per project.",
            text=(
                "Rate Limits\nLimits\n"
                "Event ingestion is limited to 600 requests per minute per project."
            ),
        ),
        Chunk(
            chunk_id="c2",
            doc_id="billing",
            doc_title="Billing",
            heading="Pricing",
            ordinal=1,
            body="Overage is billed at twelve cents per thousand events.",
            text="Billing\nPricing\nOverage is billed at twelve cents per thousand events.",
        ),
    ]
    return Index(chunks, HashingEmbedder(dim=128))


def test_mock_provider_produces_cited_sentences() -> None:
    answer = answer_question(build_index(), "How many requests per minute are allowed?")
    assert answer.provider == "mock"
    assert "[1]" in answer.text or "[2]" in answer.text
    assert answer.citations
    assert all(1 <= citation.marker <= len(answer.retrieved) for citation in answer.citations)


def test_citations_are_unique_and_ordered() -> None:
    answer = answer_question(build_index(), "What is the ingestion limit and overage price?")
    markers = [citation.marker for citation in answer.citations]
    assert markers == sorted(set(markers))


def test_out_of_range_markers_are_dropped() -> None:
    contexts = [Scored(build_index().chunks[0], 1.0, 1)]
    citations = build_citations("Supported by [1] but not [7].", contexts)
    assert [c.marker for c in citations] == [1]


def test_duplicate_markers_collapse() -> None:
    contexts = [Scored(build_index().chunks[0], 1.0, 1)]
    citations = build_citations("First [1]. Second [1].", contexts)
    assert len(citations) == 1


def test_unknown_question_returns_explicit_refusal() -> None:
    answer = answer_question(build_index(), "What is the airspeed of an unladen swallow?")
    assert "could not find" in answer.text.lower()
    assert answer.citations == []


def test_empty_question_is_rejected() -> None:
    try:
        answer_question(build_index(), "   ")
    except ValueError as exc:
        assert "empty" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_estimate_tokens_is_monotonic() -> None:
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 400) > estimate_tokens("a" * 40)


def test_retrieved_chunks_are_exposed_for_auditing() -> None:
    answer = answer_question(build_index(), "overage price", k=2)
    assert 1 <= len(answer.retrieved) <= 2
    assert answer.latency_ms >= 0.0
