"""Answer synthesis and citation extraction."""

from __future__ import annotations

import re
import time
from collections.abc import Sequence

from .models import Answer, Citation, Scored
from .providers import Provider, get_provider
from .retrieval import Index
from .text import snippet_text

_CITATION_RE = re.compile(r"\[(\d{1,2})\]")
_SNIPPET_LIMIT = 260


def build_citations(text: str, contexts: Sequence[Scored]) -> list[Citation]:
    """Map ``[n]`` markers in the answer back to the chunks that produced them."""
    citations: list[Citation] = []
    seen: set[int] = set()
    for match in _CITATION_RE.finditer(text):
        marker = int(match.group(1))
        if marker in seen or not (1 <= marker <= len(contexts)):
            continue
        seen.add(marker)
        hit = contexts[marker - 1]
        citations.append(
            Citation(
                marker=marker,
                chunk_id=hit.chunk.chunk_id,
                doc_id=hit.chunk.doc_id,
                doc_title=hit.chunk.doc_title,
                heading=hit.chunk.heading,
                snippet=snippet_text(hit.chunk.body, _SNIPPET_LIMIT),
                score=round(hit.score, 6),
            )
        )
    return citations


def answer_question(
    index: Index,
    question: str,
    provider: Provider | None = None,
    k: int = 5,
    mode: str = "auto",
    rerank: bool = False,
) -> Answer:
    """Retrieve, generate, and attach citations. Latency covers the whole pipeline."""
    question = question.strip()
    if not question:
        raise ValueError("question must not be empty")

    started = time.perf_counter()
    resolved_mode = index.resolve_mode(mode)
    hits = index.search(question, k=k, mode=resolved_mode, rerank=rerank)
    provider = provider or get_provider()
    result = provider.generate(question, hits)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    return Answer(
        question=question,
        text=result.text,
        citations=build_citations(result.text, hits),
        retrieval_mode=resolved_mode,
        reranked=rerank,
        provider=provider.name,
        latency_ms=round(elapsed_ms, 2),
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost_usd=result.cost_usd,
        retrieved=hits,
    )
