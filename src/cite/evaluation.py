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
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import CaseScore, ConfigResult, EvalCase, Scored
from .retrieval import Index

DEFAULT_K = 5

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


def answer_metrics(
    text: str, citations: Sequence, relevant_docs: Sequence[str]
) -> dict[str, float]:
    """Heuristic answer-level checks. Labeled heuristic because they are, not because they're weak.

    ``grounded_rate`` asks how many answer sentences carry at least one citation.
    ``citation_precision`` asks how many citations point at a document a human
    marked relevant. Both are proxies for faithfulness; neither replaces a human
    review, and the README says so.
    """
    from .text import split_sentences

    sentences = [s for s in split_sentences(text) if s]
    grounded = sum(1 for s in sentences if "[" in s and "]" in s)
    relevant = set(relevant_docs)

    if citations:
        cited_docs = {c.doc_id for c in citations}
        precision = len(cited_docs & relevant) / len(cited_docs)
        recall = len(cited_docs & relevant) / len(relevant) if relevant else 0.0
    else:
        precision = recall = 0.0

    return {
        "grounded_rate": grounded / len(sentences) if sentences else 0.0,
        "citation_precision": precision,
        "citation_recall": recall,
    }
