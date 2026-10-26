"""Support judges for answer evaluation.

The default judge is lexical: a cited sentence counts as supported when enough of
its content terms appear in the chunk it cites. That is cheap, deterministic, and
honest about being a proxy. It cannot recognise paraphrase and it rewards quoting.

A hosted model can judge the same claim/evidence pairs. That catches paraphrase
and should catch a sentence stitched together from two sources, but it introduces
a second model that can be wrong, non-deterministic, and paid per call. The two
are deliberately separate implementations behind one protocol so evaluation
output always states which one produced the number.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .providers import AnthropicProvider, OpenAIProvider
from .text import terms

SUPPORT_THRESHOLD = 0.60


class Judge(Protocol):
    name: str

    def supports(self, claim: str, evidence: Sequence[str]) -> bool: ...


class LexicalJudge:
    """Term-overlap judge. Fast, offline, and explicitly a proxy."""

    name = "lexical"

    def __init__(self, threshold: float = SUPPORT_THRESHOLD) -> None:
        self.threshold = threshold

    def score(self, claim: str, evidence: Sequence[str]) -> float:
        claim_terms = set(terms(claim))
        if not claim_terms or not evidence:
            return 0.0
        return max(
            len(claim_terms & set(terms(passage))) / len(claim_terms)
            for passage in evidence
        )

    def supports(self, claim: str, evidence: Sequence[str]) -> bool:
        return self.score(claim, evidence) >= self.threshold


class ProviderJudge:
    """Wrap a provider's ``judge_claim`` method in the judge protocol."""

    def __init__(self, provider) -> None:
        if not hasattr(provider, "judge_claim"):
            raise ValueError(f"provider {provider!r} does not implement judge_claim")
        self.provider = provider
        self.name = provider.name

    def supports(self, claim: str, evidence: Sequence[str]) -> bool:
        return bool(self.provider.judge_claim(claim, evidence))


def get_judge(name: str | None = None) -> Judge:
    """Resolve ``lexical``, ``openai``, or ``anthropic``.

    ``mock`` is accepted as an alias for ``lexical`` because that is the offline
    provider name and users reasonably try it.
    """
    resolved = (name or "lexical").strip().lower()
    if resolved in {"lexical", "mock", "none", ""}:
        return LexicalJudge()
    if resolved == "openai":
        return ProviderJudge(OpenAIProvider())
    if resolved == "anthropic":
        return ProviderJudge(AnthropicProvider())
    raise ValueError(
        f"unknown judge {resolved!r}; expected lexical, openai, or anthropic"
    )
