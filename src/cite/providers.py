"""Answer providers.

``MockProvider`` is the default and is not a stub: it performs extractive
summarisation with sentence-level citation, so the whole product - including the
citation UI - works with zero API keys. Hosted providers share one prompt and one
result type, so swapping them changes nothing downstream.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .models import Scored
from .text import normalize, plain_markdown, split_sentences, terms

SYSTEM_PROMPT = """You answer questions using ONLY the numbered sources provided.

Rules:
1. Every factual sentence must end with one or more citation markers like [1] or [2][3].
2. Never cite a source that does not support the sentence.
3. If the sources do not contain the answer, reply exactly:
   "I could not find that in the provided sources."
4. Be concise: at most four sentences. Do not repeat the question."""


@dataclass(frozen=True, slots=True)
class GenerationResult:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


class Provider(Protocol):
    name: str

    def generate(self, question: str, contexts: Sequence[Scored]) -> GenerationResult: ...


def estimate_tokens(text: str) -> int:
    """Rough token count. ~4 characters per token is accurate enough for cost reporting."""
    return max(1, len(text) // 4)


def format_contexts(contexts: Sequence[Scored]) -> str:
    blocks = []
    for index, hit in enumerate(contexts, start=1):
        blocks.append(f"[{index}] {hit.chunk.doc_title} > {hit.chunk.heading}\n{hit.chunk.body}")
    return "\n\n".join(blocks)


class MockProvider:
    """Deterministic extractive answerer. Selects the most query-relevant sentences."""

    name = "mock"

    def __init__(self, max_sentences: int = 3) -> None:
        self.max_sentences = max_sentences

    def generate(self, question: str, contexts: Sequence[Scored]) -> GenerationResult:
        query_terms = set(terms(question))
        if not query_terms or not contexts:
            return GenerationResult("I could not find that in the provided sources.")

        candidates: list[tuple[float, int, int, str]] = []
        for position, hit in enumerate(contexts, start=1):
            # Heading terms are part of the sentence's context even though they are
            # not part of the sentence. Without this, a chunk titled "Funnel numbers
            # changed retroactively" cannot answer a question about numbers changing,
            # because its body says "late events are inserted by timestamp".
            heading_terms = set(terms(hit.chunk.heading))
            for order, sentence in enumerate(split_sentences(hit.chunk.body)):
                # Short sentences are often the *best* answers ("50 tiles per
                # dashboard"). Only skip fragments that carry no content.
                if len(sentence) < 12:
                    continue
                # Table rows are grounded but read terribly as prose.
                if sentence.startswith("|"):
                    continue
                sentence_terms = set(terms(sentence))
                overlap = len(query_terms & (sentence_terms | heading_terms))
                if overlap == 0:
                    continue
                coverage = overlap / len(query_terms)
                # Earlier ranks and earlier sentences are slightly preferred; ties
                # break deterministically so evaluation runs are reproducible.
                score = coverage + 0.20 * (1.0 / position) - 0.01 * order
                candidates.append((score, position, order, sentence))

        if not candidates:
            return GenerationResult("I could not find that in the provided sources.")

        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        chosen: list[tuple[int, int, str]] = []
        seen: set[str] = set()
        best_score = candidates[0][0]
        for score, position, order, sentence in candidates:
            # Stay focused: once a clearly best chunk exists, do not dilute the
            # answer with sentences that score much lower elsewhere.
            if chosen and score < best_score * 0.75:
                break
            key = normalize(sentence)[:60].lower()
            if key in seen:
                continue
            seen.add(key)
            chosen.append((position, order, sentence))
            if len(chosen) >= self.max_sentences:
                break

        # Preserve source order so the answer reads like prose rather than a ranking.
        chosen.sort(key=lambda item: (item[0], item[1]))
        text = " ".join(
            f"{plain_markdown(sentence)} [{position}]" for position, _, sentence in chosen
        )
        prompt_tokens = estimate_tokens(question) + sum(
            estimate_tokens(hit.chunk.body) for hit in contexts
        )
        return GenerationResult(
            text=text,
            tokens_in=prompt_tokens,
            tokens_out=estimate_tokens(text),
            cost_usd=0.0,
        )


class OpenAIProvider:
    """Chat completions. Reads the key at call time so tests never need one."""

    name = "openai"
    input_cost_per_million = 0.15
    output_cost_per_million = 0.60

    def __init__(self, model: str = "gpt-4o-mini") -> None:
        self.model = model

    def generate(self, question: str, contexts: Sequence[Scored]) -> GenerationResult:
        import httpx

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set; use CITE_LLM_PROVIDER=mock")

        payload = {
            "model": self.model,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Sources:\n\n{format_contexts(contexts)}\n\nQuestion: {question}",
                },
            ],
        }
        with httpx.Client(timeout=90.0) as client:
            response = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()

        usage = body.get("usage", {})
        tokens_in = int(usage.get("prompt_tokens", 0))
        tokens_out = int(usage.get("completion_tokens", 0))
        return GenerationResult(
            text=body["choices"][0]["message"]["content"].strip(),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=(
                tokens_in / 1_000_000 * self.input_cost_per_million
                + tokens_out / 1_000_000 * self.output_cost_per_million
            ),
        )


class AnthropicProvider:
    name = "anthropic"
    input_cost_per_million = 0.80
    output_cost_per_million = 4.00

    def __init__(self, model: str = "claude-3-5-haiku-latest") -> None:
        self.model = model

    def generate(self, question: str, contexts: Sequence[Scored]) -> GenerationResult:
        import httpx

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set; use CITE_LLM_PROVIDER=mock")

        payload = {
            "model": self.model,
            "max_tokens": 512,
            "temperature": 0.0,
            "system": SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": f"Sources:\n\n{format_contexts(contexts)}\n\nQuestion: {question}",
                }
            ],
        }
        with httpx.Client(timeout=90.0) as client:
            response = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            body = response.json()

        usage = body.get("usage", {})
        tokens_in = int(usage.get("input_tokens", 0))
        tokens_out = int(usage.get("output_tokens", 0))
        text = "".join(
            block.get("text", "")
            for block in body.get("content", [])
            if block.get("type") == "text"
        ).strip()
        return GenerationResult(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=(
                tokens_in / 1_000_000 * self.input_cost_per_million
                + tokens_out / 1_000_000 * self.output_cost_per_million
            ),
        )


def get_provider(name: str | None = None) -> Provider:
    resolved = (name or os.getenv("CITE_LLM_PROVIDER") or "mock").strip().lower()
    if resolved == "mock":
        return MockProvider()
    if resolved == "openai":
        return OpenAIProvider()
    if resolved == "anthropic":
        return AnthropicProvider()
    raise ValueError(f"unknown provider {resolved!r}; expected mock, openai, or anthropic")
