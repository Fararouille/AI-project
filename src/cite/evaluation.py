"""The reason this project exists: measurable retrieval quality.

Most RAG demos show an answer and stop. That tells you nothing about whether the
system is any good. This module grades retrieval against a golden set and reports
metrics that are standard in the information-retrieval literature:

* recall@k - of the documents that should be found, how many made it into the top k
* MRR      - how high the first relevant document ranks, averaged over queries
* nDCG@k   - rank-weighted quality, so position 1 counts more than position 5

Metrics are computed at *document* level. Chunks are an implementation detail; a
user cares whether the right page was found, not which slice of it.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from .answer import answer_question
from .judges import SUPPORT_THRESHOLD, Judge, LexicalJudge
from .models import (
    Answer,
    AnswerCaseScore,
    AnswerResult,
    CaseScore,
    ConfigResult,
    EvalCase,
    Scored,
)
from .providers import Provider, get_provider
from .retrieval import Index

DEFAULT_K = 5
CITATION_RE = re.compile(r"\[(\d{1,2})\]")
REFUSAL_MARKER = "could not find"


DEFAULT_CONFIGS: tuple[tuple[str, str, bool], ...] = (
    ("BM25 only", "bm25", False),
    ("Dense only", "dense", False),
    ("Hybrid (RRF)", "hybrid", False),
    ("Hybrid + rerank", "hybrid", True),
)


@dataclass(frozen=True, slots=True)
class GoldenSet:
    cases: tuple[EvalCase, ...]
    k: int


def load_golden(path: str | Path) -> GoldenSet:
    """Read a YAML golden set. Fails loudly on malformed data rather than skipping cases."""
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError(f"{path}: expected a non-empty 'cases' list")

    cases: list[EvalCase] = []
    for index, item in enumerate(raw_cases):
        if "question" not in item or "relevant" not in item:
            raise ValueError(f"{path}: case #{index} needs 'question' and 'relevant'")
        relevant = item["relevant"]
        if isinstance(relevant, str):
            relevant = [relevant]
        cases.append(
            EvalCase(
                case_id=str(item.get("id") or f"case-{index + 1}"),
                question=str(item["question"]).strip(),
                relevant_docs=tuple(str(doc) for doc in relevant),
                note=str(item.get("note", "")),
                expects=tuple(str(fact) for fact in item.get("expects", [])),
            )
        )
    return GoldenSet(cases=tuple(cases), k=int(payload.get("k", DEFAULT_K)))


def doc_ranking(hits: Sequence[Scored]) -> list[str]:
    """Collapse a chunk ranking into a document ranking, preserving best position."""
    seen: set[str] = set()
    ordered: list[str] = []
    for hit in hits:
        if hit.chunk.doc_id not in seen:
            seen.add(hit.chunk.doc_id)
            ordered.append(hit.chunk.doc_id)
    return ordered


def recall_at_k(ranked_docs: Sequence[str], relevant: Sequence[str], k: int) -> float:
    if not relevant:
        return 0.0
    found = set(ranked_docs[:k]) & set(relevant)
    return len(found) / len(set(relevant))


def reciprocal_rank(ranked_docs: Sequence[str], relevant: Sequence[str]) -> float:
    relevant_set = set(relevant)
    for position, doc_id in enumerate(ranked_docs, start=1):
        if doc_id in relevant_set:
            return 1.0 / position
    return 0.0


def ndcg_at_k(ranked_docs: Sequence[str], relevant: Sequence[str], k: int) -> float:
    """Binary-gain nDCG. Ideal ranking puts every relevant doc first."""
    if not relevant:
        return 0.0
    relevant_set = set(relevant)
    gains = [1.0 if doc_id in relevant_set else 0.0 for doc_id in ranked_docs[:k]]
    dcg = sum(gain / math.log2(position + 2) for position, gain in enumerate(gains))
    ideal = sum(
        1.0 / math.log2(position + 2) for position in range(min(k, len(relevant_set)))
    )
    return dcg / ideal if ideal else 0.0


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile. No numpy dependency, sensible at small n."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((p / 100.0) * (len(ordered) - 1))))
    return float(ordered[index])


def run_config(
    index: Index,
    cases: Sequence[EvalCase],
    name: str,
    mode: str,
    rerank: bool,
    k: int = DEFAULT_K,
) -> ConfigResult:
    scores: list[CaseScore] = []
    for case in cases:
        started = time.perf_counter()
        hits = index.search(case.question, k=k, mode=mode, rerank=rerank)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        ranking = doc_ranking(hits)
        scores.append(
            CaseScore(
                case_id=case.case_id,
                recall_at_k=recall_at_k(ranking, case.relevant_docs, k),
                reciprocal_rank=reciprocal_rank(ranking, case.relevant_docs),
                ndcg_at_k=ndcg_at_k(ranking, case.relevant_docs, k),
                latency_ms=elapsed_ms,
            )
        )

    count = len(scores)
    return ConfigResult(
        name=name,
        k=k,
        recall_at_k=sum(s.recall_at_k for s in scores) / count,
        mrr=sum(s.reciprocal_rank for s in scores) / count,
        ndcg_at_k=sum(s.ndcg_at_k for s in scores) / count,
        latency_p50_ms=percentile([s.latency_ms for s in scores], 50),
        latency_p95_ms=percentile([s.latency_ms for s in scores], 95),
        cases=scores,
    )


def run_ablations(
    index: Index,
    cases: Sequence[EvalCase],
    k: int = DEFAULT_K,
    configs: Sequence[tuple[str, str, bool]] = DEFAULT_CONFIGS,
) -> list[ConfigResult]:
    return [run_config(index, cases, name, mode, rerank, k) for name, mode, rerank in configs]


def render_markdown(results: Sequence[ConfigResult], embedder_name: str = "") -> str:
    """Produce a table that can be pasted straight into a README."""
    header = "| Configuration | Recall@k | MRR | nDCG@k | p50 (ms) | p95 (ms) |"
    divider = "|---|---:|---:|---:|---:|---:|"
    lines = [header, divider]
    for result in results:
        lines.append(
            f"| {result.name} | {result.recall_at_k:.3f} | {result.mrr:.3f} | "
            f"{result.ndcg_at_k:.3f} | {result.latency_p50_ms:.2f} | "
            f"{result.latency_p95_ms:.2f} |"
        )
    if results:
        lines.append("")
        suffix = f" (embedder: {embedder_name})" if embedder_name else ""
        lines.append(f"k = {results[0].k}, {len(results[0].cases)} queries{suffix}.")
    return "\n".join(lines)


def _split_cited_sentences(text: str) -> list[str]:
    """Split an answer into sentences while keeping citation markers attached.

    ``"Claim one. [1] Claim two. [2]"`` is the format both the mock and the hosted
    prompt produce. A naive split at ``". "`` would start the next sentence with
    ``[1]`` and report the first claim as ungrounded. Peeking one token ahead
    fixes that without special-casing the provider.
    """
    tokens = text.split()
    sentences: list[str] = []
    current: list[str] = []
    position = 0
    while position < len(tokens):
        token = tokens[position]
        current.append(token)
        if re.search(r"[.!?][\"')\]]*$", token):
            while position + 1 < len(tokens) and re.fullmatch(
                r"(?:\[\d{1,2}\])+", tokens[position + 1]
            ):
                position += 1
                current.append(tokens[position])
            sentences.append(" ".join(current))
            current = []
        position += 1
    if current:
        sentences.append(" ".join(current))
    return [sentence for sentence in sentences if sentence.strip()]


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _cited_contexts(sentence: str, answer: Answer) -> list[Scored]:
    markers = [int(marker) for marker in CITATION_RE.findall(sentence)]
    return [
        answer.retrieved[marker - 1]
        for marker in markers
        if 1 <= marker <= len(answer.retrieved)
    ]


def _sentence_support(sentence: str, answer: Answer) -> float:
    """Best lexical overlap between one sentence and the chunks it cites."""
    contexts = _cited_contexts(sentence, answer)
    if not contexts:
        return 0.0
    claim = CITATION_RE.sub(" ", sentence)
    return LexicalJudge().score(claim, [hit.chunk.text for hit in contexts])


def score_answer(
    answer: Answer, case: EvalCase, judge: Judge | None = None
) -> AnswerCaseScore:
    """Grade one answer with citation and support metrics.

    ``judge`` defaults to the lexical proxy. Passing a model judge changes only
    the support columns; citation precision/recall and expected-fact recall are
    computed from labels and text, not from the judge.
    """
    judge = judge or LexicalJudge()
    sentences = _split_cited_sentences(answer.text)
    answered = bool(sentences) and REFUSAL_MARKER not in answer.text.casefold()
    if not answered:
        return AnswerCaseScore(
            case_id=case.case_id,
            answered=False,
            grounded_rate=0.0,
            citation_precision=0.0,
            citation_recall=0.0,
            support_rate=0.0,
            unsupported_claim_rate=0.0,
            fact_recall=0.0,
        )

    grounded = sum(1 for sentence in sentences if CITATION_RE.search(sentence))
    supported = 0
    for sentence in sentences:
        contexts = _cited_contexts(sentence, answer)
        claim = CITATION_RE.sub(" ", sentence).strip()
        if contexts and judge.supports(claim, [hit.chunk.text for hit in contexts]):
            supported += 1

    relevant = set(case.relevant_docs)
    cited = {citation.doc_id for citation in answer.citations}
    precision = len(cited & relevant) / len(cited) if cited else 0.0
    recall = len(cited & relevant) / len(relevant) if relevant else 0.0

    if case.expects:
        haystack = _normalized(answer.text)
        matched = sum(1 for fact in case.expects if _normalized(fact) in haystack)
        fact_recall = matched / len(case.expects)
    else:
        fact_recall = 0.0

    return AnswerCaseScore(
        case_id=case.case_id,
        answered=True,
        grounded_rate=grounded / len(sentences),
        citation_precision=precision,
        citation_recall=recall,
        support_rate=supported / len(sentences),
        unsupported_claim_rate=1.0 - supported / len(sentences),
        fact_recall=fact_recall,
    )


def run_answer_eval(
    index: Index,
    cases: Sequence[EvalCase],
    provider: Provider | None = None,
    k: int = DEFAULT_K,
    mode: str = "auto",
    rerank: bool = False,
    name: str | None = None,
    judge: Judge | None = None,
) -> AnswerResult:
    """Run the answer pipeline over the golden set and aggregate its checks."""
    provider = provider or get_provider()
    judge = judge or LexicalJudge()
    resolved_mode = index.resolve_mode(mode)
    scores = [
        score_answer(
            answer_question(
                index,
                case.question,
                provider=provider,
                k=k,
                mode=resolved_mode,
                rerank=rerank,
            ),
            case,
            judge=judge,
        )
        for case in cases
    ]

    count = len(scores)
    fact_scores = [
        score.fact_recall for score, case in zip(scores, cases, strict=True) if case.expects
    ]
    return AnswerResult(
        name=name or f"{provider.name} provider",
        provider=provider.name,
        mode=resolved_mode,
        reranked=rerank,
        k=k,
        answered_rate=sum(score.answered for score in scores) / count,
        grounded_rate=sum(score.grounded_rate for score in scores) / count,
        citation_precision=sum(score.citation_precision for score in scores) / count,
        citation_recall=sum(score.citation_recall for score in scores) / count,
        support_rate=sum(score.support_rate for score in scores) / count,
        unsupported_claim_rate=sum(score.unsupported_claim_rate for score in scores) / count,
        fact_recall=(sum(fact_scores) / len(fact_scores)) if fact_scores else 0.0,
        fact_cases=len(fact_scores),
        cases=scores,
        judge=judge.name,
    )


def render_answer_markdown(results: Sequence[AnswerResult]) -> str:
    """Answer-quality table. Kept separate from retrieval metrics on purpose."""
    header = (
        "| Configuration | Answered | Grounded | Citation P | Citation R | "
        f"{'Lexical support' if not results or results[0].judge == 'lexical' else 'Model support'} "
        "| Unsupported | Expected facts |"
    )
    divider = "|---|---:|---:|---:|---:|---:|---:|---:|"
    lines = [header, divider]
    for result in results:
        lines.append(
            f"| {result.name} | {result.answered_rate:.3f} | {result.grounded_rate:.3f} | "
            f"{result.citation_precision:.3f} | {result.citation_recall:.3f} | "
            f"{result.support_rate:.3f} | {result.unsupported_claim_rate:.3f} | "
            f"{result.fact_recall:.3f} |"
        )
    if results:
        first = results[0]
        lines.append("")
        lines.append(
            f"k = {first.k}, {len(first.cases)} queries, provider = {first.provider}, "
            f"mode = {first.mode}, support judge = {first.judge}. Expected-fact recall "
            f"is averaged over {first.fact_cases} questions that declare `expects:`."
        )
        if first.judge == "lexical":
            lines.append(
                "Lexical support is a proxy: a sentence counts as supported when at least "
                f"{SUPPORT_THRESHOLD:.0%} of its content terms appear in the chunk it cites. "
                "It catches unsupported or mis-cited sentences, not every form of "
                "hallucination; see the README's limitations."
            )
        else:
            lines.append(
                f"Support was judged by `{first.judge}`, a model with its own errors and "
                "cost. A model judgement is not ground truth and is not reproducible "
                "across model versions; compare it with the lexical column before "
                "trusting a change."
            )
    return "\n".join(lines)


def answer_metrics(
    text: str, citations: Sequence, relevant_docs: Sequence[str]
) -> dict[str, float]:
    """Heuristic answer-level checks that only need text and citations.

    Kept as a small public helper for callers that do not have the retrieved
    chunks. :func:`score_answer` is the fuller version used by the harness.
    """
    sentences = _split_cited_sentences(text)
    grounded = sum(1 for sentence in sentences if CITATION_RE.search(sentence))
    relevant = set(relevant_docs)

    if citations:
        cited_docs = {citation.doc_id for citation in citations}
        precision = len(cited_docs & relevant) / len(cited_docs)
        recall = len(cited_docs & relevant) / len(relevant) if relevant else 0.0
    else:
        precision = recall = 0.0

    return {
        "grounded_rate": grounded / len(sentences) if sentences else 0.0,
        "citation_precision": precision,
        "citation_recall": recall,
    }
