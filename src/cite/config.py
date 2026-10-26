"""Runtime configuration. Explicit defaults, environment overrides, no hidden magic."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    corpus_dir: Path
    index_path: Path
    golden_path: Path

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            corpus_dir=Path(os.getenv("CITE_CORPUS_DIR", "data/corpus")),
            index_path=Path(os.getenv("CITE_INDEX_PATH", "data/index.json")),
            golden_path=Path(os.getenv("CITE_GOLDEN_PATH", "eval/golden.yaml")),
        )
