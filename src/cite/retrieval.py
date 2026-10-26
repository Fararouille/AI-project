"""Retrieval: an inverted-index BM25, a dense index, rank fusion, and a reranker.

Three retrievers are exposed so the evaluation harness can compare them honestly:

* ``bm25``   - lexical, inverted index, no training, very hard to beat on exact terms
* ``dense``  - cosine similarity over embedder output
* ``hybrid`` - reciprocal rank fusion of the two, which sidesteps score calibration

Fusion happens at the *rank* level rather than the score level on purpose. BM25
scores are unbounded and dense cosine scores live in [-1, 1]; adding them directly
requires tuning a weight per corpus. RRF has no such knob.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence

from .embeddings import Embedder, get_embedder
from .models import Chunk, Scored
from .text import stem, terms

RRF_K = 60


class BM25Index:
    """Okapi BM25 over an inverted index.

    Only documents that contain at least one query term are scored, so query cost
    scales with postings touched rather than corpus size.
    """

    def __init__(self, chunks: Sequence[Chunk], k1: float = 1.5, b: float = 0.75) -> None:
        self.chunks = list(chunks)
        self.k1 = k1
        self.b = b

        term_freqs = [Counter(terms(chunk.text)) for chunk in self.chunks]
        self._lengths = [sum(tf.values()) for tf in term_freqs]
        self._avgdl = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0

        postings: dict[str, list[tuple[int, int]]] = {}
        for position, tf in enumerate(term_freqs):
            for term, freq in tf.items():
                postings.setdefault(term, []).append((position, freq))
        self._postings = postings

        total = len(self.chunks)
        self._idf = {
            term: math.log(1.0 + (total - len(p) + 0.5) / (len(p) + 0.5))
            for term, p in postings.items()
        }
        self._default_idf = math.log(1.0 + (total + 0.5) / 0.5) if total else 0.0

    def idf(self, term: str) -> float:
        """Public helper. Accepts a raw word and stems it, so callers cannot silently
        get the default IDF by passing an unstemmed term."""
        return self._idf.get(stem(term), self._default_idf)

    def search(self, query: str, k: int = 10) -> list[Scored]:
        query_terms = terms(query)
        if not query_terms or not self.chunks:
            return []

        scores: dict[int, float] = {}
        avgdl = self._avgdl or 1.0
        for term in query_terms:
            postings = self._postings.get(term)
            if not postings:
                continue
            idf = self._idf[term]
            for position, freq in postings:
                norm = self.k1 * (1.0 - self.b + self.b * self._lengths[position] / avgdl)
                contribution = idf * (freq * (self.k1 + 1.0)) / (freq + norm)
                scores[position] = scores.get(position, 0.0) + contribution

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:k]
        return [
            Scored(chunk=self.chunks[position], score=score, rank=rank)
            for rank, (position, score) in enumerate(ranked, start=1)
            if score > 0.0
        ]


class DenseIndex:
    """Cosine similarity over pre-normalised vectors: a dot product is enough."""

    def __init__(self, chunks: Sequence[Chunk], embedder: Embedder | None = None) -> None:
        self.chunks = list(chunks)
        self.embedder = embedder or get_embedder()
        self._vectors = self.embedder.embed([c.text for c in self.chunks]) if self.chunks else []

    def search(self, query: str, k: int = 10) -> list[Scored]:
        if not self.chunks:
            return []
        query_vector = self.embedder.embed([query])[0]
        scored = [
            (position, sum(a * b for a, b in zip(query_vector, vector, strict=False)))
            for position, vector in enumerate(self._vectors)
        ]
        scored.sort(key=lambda item: item[1], reverse=True)
        return [
            Scored(chunk=self.chunks[position], score=score, rank=rank)
            for rank, (position, score) in enumerate(scored[:k], start=1)
        ]


def reciprocal_rank_fusion(
    result_lists: Iterable[Sequence[Scored]], rrf_k: int = RRF_K
) -> list[Scored]:
    """Fuse ranked lists. A chunk's fused score is sum(1 / (k + rank)) across lists."""
    fused: dict[str, float] = {}
    by_id: dict[str, Chunk] = {}
    for results in result_lists:
        for hit in results:
            by_id[hit.chunk.chunk_id] = hit.chunk
            fused[hit.chunk.chunk_id] = fused.get(hit.chunk.chunk_id, 0.0) + 1.0 / (
                rrf_k + hit.rank
            )
    ordered = sorted(fused.items(), key=lambda item: item[1], reverse=True)
    return [
        Scored(chunk=by_id[chunk_id], score=score, rank=rank)
        for rank, (chunk_id, score) in enumerate(ordered, start=1)
    ]


class HeuristicReranker:
    """A cheap second stage that rewards term overlap and heading agreement.

    This is a stand-in for a cross-encoder. It exists to show the *shape* of a
    reranking stage and to be measured against no-reranking, not to claim
    cross-encoder quality. ``Reranker`` is a one-method protocol so a real model
    can be dropped in without touching call sites.
    """

    def __init__(self) -> None:
        self.name = "heuristic"

    @staticmethod
    def _blend(query: str, hit: Scored, base: float) -> float:
        """``base`` is the min-max normalised first-stage score, not the raw score.

        Blending raw RRF scores (which live around 1/61) with [0, 1] overlap
        features would let the lexical terms dominate completely and quietly turn
        the reranker into "ignore retrieval, sort by word overlap".
        """
        query_terms = set(terms(query))
        if not query_terms:
            return base
        chunk_terms = set(terms(hit.chunk.text))
        heading_terms = set(terms(hit.chunk.heading))
        overlap = len(query_terms & chunk_terms) / len(query_terms)
        heading = len(query_terms & heading_terms) / len(query_terms)
        lowered = hit.chunk.body.lower()
        phrase = 1.0 if query.lower().strip(" ?.") in lowered else 0.0
        return 0.55 * base + 0.30 * overlap + 0.10 * heading + 0.05 * phrase

    def rerank(self, query: str, hits: Sequence[Scored], k: int) -> list[Scored]:
        if not hits:
            return []
        scores = [hit.score for hit in hits]
        low, high = min(scores), max(scores)
        span = high - low
        rescored = [
            (hit, self._blend(query, hit, 1.0 if span == 0 else (hit.score - low) / span))
            for hit in hits
        ]
        rescored.sort(key=lambda item: item[1], reverse=True)
        return [
            Scored(chunk=hit.chunk, score=score, rank=rank)
            for rank, (hit, score) in enumerate(rescored[:k], start=1)
        ]


class Index:
    """Facade over the retrievers, with persistence and a single ``search`` entry point."""

    def __init__(self, chunks: Sequence[Chunk], embedder: Embedder | None = None) -> None:
        self.chunks = list(chunks)
        self.embedder = embedder or get_embedder()
        self.bm25 = BM25Index(self.chunks)
        self._dense: DenseIndex | None = None
        self.reranker = HeuristicReranker()

    @property
    def dense(self) -> DenseIndex:
        if self._dense is None:
            self._dense = DenseIndex(self.chunks, self.embedder)
        return self._dense

    def resolve_mode(self, mode: str) -> str:
        """Resolve ``auto`` to the retriever the current embedder supports.

        The offline hashing embedder is lexical, so fusing it with BM25 adds noise
        rather than signal - the golden set shows exactly that. A real embedding
        model makes dense retrieval worth fusing, so ``auto`` turns hybrid on only
        when the embedder is semantic.
        """
        if mode != "auto":
            return mode
        return "bm25" if self.embedder.name == "hashing" else "hybrid"

    def __len__(self) -> int:
        return len(self.chunks)

    def search(
        self,
        query: str,
        k: int = 5,
        mode: str = "auto",
        rerank: bool = False,
        pool: int | None = None,
    ) -> list[Scored]:
        """Retrieve ``k`` chunks. ``pool`` controls how many candidates each retriever emits."""
        if not self.chunks:
            return []
        mode = self.resolve_mode(mode)
        # Retrieve a deeper candidate pool than k. Rank fusion and the reranker
        # both need alternatives to promote; when depth == k the fusion stage can
        # only shuffle the five results it was already given.
        depth = pool or max(k * 4, 20)

        if mode == "bm25":
            hits = self.bm25.search(query, depth)
        elif mode == "dense":
            hits = self.dense.search(query, depth)
        elif mode == "hybrid":
            hits = reciprocal_rank_fusion(
                [self.bm25.search(query, depth), self.dense.search(query, depth)]
            )[:depth]
        else:
            raise ValueError(f"unknown retrieval mode {mode!r}")

        if rerank and hits:
            return self.reranker.rerank(query, hits, k)
        return hits[:k]

    # -- persistence -------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "version": 1,
            "embedder": self.embedder.name,
            "dim": self.embedder.dim,
            "chunks": [
                {
                    "chunk_id": c.chunk_id,
                    "doc_id": c.doc_id,
                    "doc_title": c.doc_title,
                    "heading": c.heading,
                    "ordinal": c.ordinal,
                    "body": c.body,
                    "text": c.text,
                    "source": c.source,
                }
                for c in self.chunks
            ],
        }

    @classmethod
    def from_dict(cls, payload: dict, embedder: Embedder | None = None) -> Index:
        chunks = [
            Chunk(
                chunk_id=item["chunk_id"],
                doc_id=item["doc_id"],
                doc_title=item["doc_title"],
                heading=item.get("heading", ""),
                ordinal=item.get("ordinal", 0),
                body=item["body"],
                text=item["text"],
                source=item.get("source", ""),
            )
            for item in payload.get("chunks", [])
        ]
        return cls(chunks, embedder)


def normalize_scores(hits: Sequence[Scored]) -> list[Scored]:
    """Min-max normalise scores into [0, 1]; useful for display, never for ranking."""
    if not hits:
        return []
    values = [hit.score for hit in hits]
    low, high = min(values), max(values)
    if math.isclose(low, high):
        return [Scored(hit.chunk, 1.0, hit.rank) for hit in hits]
    return [Scored(hit.chunk, (hit.score - low) / (high - low), hit.rank) for hit in hits]
