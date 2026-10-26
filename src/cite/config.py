"""Runtime configuration. Explicit defaults, environment overrides, no hidden magic.

Auth is off when ``CITE_API_KEYS`` is unset, which keeps local development and the
test suite zero-config. Setting one or more comma-separated keys turns auth on for
every endpoint that can spend money or mutate the index.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_COLLECTION_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TRUE = {"1", "true", "yes", "on"}


def normalize_collection(name: str | None, default: str = "default") -> str:
    value = (name or default).strip().lower()
    if not _COLLECTION_RE.match(value):
        raise ValueError(
            f"invalid collection name {value!r}; use lowercase letters, digits, "
            "'-' or '_' (max 64 characters)"
        )
    return value


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    corpus_dir: Path
    index_path: Path
    golden_path: Path
    collections_dir: Path = Path("data/collections")
    default_collection: str = "default"
    api_keys: tuple[str, ...] = ()
    rate_limit_per_minute: int = 60
    usage_db_path: Path = Path("data/usage.db")
    log_questions: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        keys = tuple(
            key.strip()
            for key in os.getenv("CITE_API_KEYS", "").split(",")
            if key.strip()
        )
        return cls(
            corpus_dir=Path(os.getenv("CITE_CORPUS_DIR", "data/corpus")),
            index_path=Path(os.getenv("CITE_INDEX_PATH", "data/index.json")),
            golden_path=Path(os.getenv("CITE_GOLDEN_PATH", "eval/golden.yaml")),
            collections_dir=Path(os.getenv("CITE_COLLECTIONS_DIR", "data/collections")),
            default_collection=normalize_collection(
                os.getenv("CITE_COLLECTION", "default")
            ),
            api_keys=keys,
            rate_limit_per_minute=_int_env("CITE_RATE_LIMIT_PER_MINUTE", 60),
            usage_db_path=Path(os.getenv("CITE_USAGE_DB", "data/usage.db")),
            log_questions=os.getenv("CITE_LOG_QUESTIONS", "").strip().lower() in _TRUE,
        )

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_keys)

    def index_path_for(self, collection: str | None = None) -> Path:
        """Default collection stays at ``CITE_INDEX_PATH``; named ones get a directory."""
        name = normalize_collection(collection, self.default_collection)
        if name == self.default_collection:
            return self.index_path
        return self.collections_dir / f"{name}.json"

    def collection_names(self) -> list[str]:
        """Collections that have an index file on disk, default first."""
        names = [self.default_collection]
        if self.collections_dir.exists():
            for path in sorted(self.collections_dir.glob("*.json")):
                if path.stem not in names:
                    names.append(path.stem)
        return names
