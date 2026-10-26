"""Plug-and-play embedding backends.

The default embedder hashes word, bigram, and character n-gram features into a
fixed-width vector. It is *not* a semantic model, and the README says so plainly.
Its purpose is that ``pip install -e . && cite eval`` produces real numbers on a
laptop with no API key and no network. Swapping in a hosted model is one env var.

Signed hashing (the +1/-1 trick from Weinberger et al.) keeps collisions from
systematically inflating similarity, which a plain modulo-hash would do.
"""

from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from .text import stem, tokenize

DEFAULT_DIM = 512


@runtime_checkable
class Embedder(Protocol):
    name: str
    dim: int
    cost_per_million_tokens: float

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return vector
    return [v / norm for v in vector]


class HashingEmbedder:
    """Deterministic, dependency-free feature hashing embedder."""

    name = "hashing"
    cost_per_million_tokens = 0.0

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.dim = dim

    def _features(self, text: str) -> list[str]:
        tokens = tokenize(text)
        features: list[str] = []
        features.extend("w:" + stem(t) for t in tokens)
        features.extend("p:" + t[:4] for t in tokens if len(t) > 3)
        features.extend(
            f"b:{tokens[i]}_{tokens[i + 1]}" for i in range(len(tokens) - 1)
        )
        padded = " " + text.lower() + " "
        for width in (4, 5):
            features.extend(
                f"c{width}:{padded[i : i + width]}"
                for i in range(len(padded) - width + 1)
            )
        return features

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dim
            for feature in self._features(text):
                digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
                value = int.from_bytes(digest, "big")
                sign = 1.0 if value & 1 else -1.0
                vector[(value >> 1) % self.dim] += sign
            vectors.append(_unit(vector))
        return vectors


class OpenAIEmbedder:
    """Hosted embeddings via the OpenAI API. Only constructed when explicitly requested."""

    name = "openai"
    cost_per_million_tokens = 0.02  # text-embedding-3-small, USD

    def __init__(self, model: str = "text-embedding-3-small", dim: int = 1536) -> None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set; cannot use the OpenAI embedder")
        self._api_key = api_key
        self.model = model
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        import httpx

        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self.model, "input": list(texts)},
            )
            response.raise_for_status()
            payload = response.json()
        ordered = sorted(payload["data"], key=lambda item: item["index"])
        return [_unit(item["embedding"]) for item in ordered]


def get_embedder(name: str | None = None) -> Embedder:
    """Resolve an embedder by name, defaulting to the offline hashing model."""
    resolved = (name or os.getenv("CITE_EMBEDDER") or "hashing").strip().lower()
    if resolved == "hashing":
        return HashingEmbedder()
    if resolved == "openai":
        return OpenAIEmbedder()
    raise ValueError(f"unknown embedder {resolved!r}; expected 'hashing' or 'openai'")
