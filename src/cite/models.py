"""Typed contracts shared by ingestion, retrieval, answering, and evaluation.

Plain dataclasses are used internally so the hot paths stay cheap; the HTTP layer
converts them to pydantic models at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

RetrievalMode = Literal["bm25", "dense", "hybrid"]


@dataclass(frozen=True, slots=True)
class Document:
    """A single source document before chunking."""

    doc_id: str
    title: str
    text: str
    source: str = ""


@dataclass(frozen=True, slots=True)
class Chunk:
    """A retrievable unit of text.

    ``text`` is what gets indexed: it carries the heading breadcrumb so that a
    passage about "rate limits" is still findable from a document about the API.
    ``body`` is what gets shown to a reader and to the language model.
    """

    chunk_id: str
    doc_id: str
    doc_title: str
    heading: str
    ordinal: int
    body: str
    text: str
    source: str = ""


@dataclass(frozen=True, slots=True)
class Scored:
    """A chunk plus the score that put it in the result list."""

    chunk: Chunk
    score: float
    rank: int


@dataclass(frozen=True, slots=True)
class Citation:
    """A pointer from an answer sentence back to its evidence."""

    marker: int
    chunk_id: str
    doc_id: str
    doc_title: str
    heading: str
    snippet: str
    score: float


@dataclass(slots=True)
class Answer:
    """An answer plus everything needed to audit it."""

    question: str
    text: str
    citations: list[Citation]
    retrieval_mode: str
    reranked: bool
    provider: str
    latency_ms: float
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    retrieved: list[Scored] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One graded query in the golden set.

    ``expects`` holds short strings that a correct answer must contain. They are
    not a full reference answer; they are the facts a reviewer would check first.
    """

    case_id: str
    question: str
    relevant_docs: tuple[str, ...]
    note: str = ""
    expects: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CaseScore:
    """Per-query metrics for a single retrieval configuration."""

    case_id: str
    recall_at_k: float
    reciprocal_rank: float
    ndcg_at_k: float
    latency_ms: float


@dataclass(slots=True)
class ConfigResult:
    """Aggregated metrics for one retrieval configuration."""

    name: str
    k: int
    recall_at_k: float
    mrr: float
    ndcg_at_k: float
    latency_p50_ms: float
    latency_p95_ms: float
    cases: list[CaseScore] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AnswerCaseScore:
    """Per-question answer quality, measured against the same golden set."""

    case_id: str
    answered: bool
    grounded_rate: float
    citation_precision: float
    citation_recall: float
    support_rate: float
    unsupported_claim_rate: float
    fact_recall: float


@dataclass(slots=True)
class AnswerResult:
    """Aggregated answer-level metrics for one provider/retrieval configuration."""

    name: str
    provider: str
    mode: str
    reranked: bool
    k: int
    answered_rate: float
    grounded_rate: float
    citation_precision: float
    citation_recall: float
    support_rate: float
    unsupported_claim_rate: float
    fact_recall: float
    fact_cases: int
    cases: list[AnswerCaseScore] = field(default_factory=list)
