"""Runtime configuration. Explicit defaults, environment overrides, no hidden magic.

Auth is off when ``CITE_API_KEYS`` is unset, which keeps local development and the
test suite zero-config. Setting one or more comma-separated keys turns auth on for
every endpoint that can spend money or mutate the index.

Two optional maps turn the single-key model into something a team can run:

* ``CITE_ADMIN_KEYS`` - keys allowed to reindex, run evaluation, and read usage
* ``CITE_KEY_ACLS``   - per-key collection grants, e.g.
  ``reader-key:handbook,support;legal-key:legal``. ``*`` grants everything.

When auth is enabled but neither map is set, every key keeps the historical
"read everything, mutate everything" behaviour. Setting either map switches the
service to least privilege: unknown keys are readers with no collections.
"""

from __future__ import annotations

import hmac
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_COLLECTION_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TRUE = {"1", "true", "yes", "on"}
_INDEX_SUFFIXES = (".json", ".sqlite3", ".sqlite", ".db")
Role = Literal["admin", "reader"]


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


def _choice_env(name: str, default: str, choices: set[str]) -> str:
    raw = (os.getenv(name) or default).strip().lower()
    if raw not in choices:
        options = ", ".join(sorted(choices))
        raise ValueError(f"{name} must be one of {options}, got {raw!r}")
    return raw


def _split_keys(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def parse_key_acls(raw: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Parse ``key:collection,collections;key2:*`` into a stable tuple.

    Keys are compared with :func:`hmac.compare_digest`, so they must not contain
    the two delimiters. Everything else is accepted; long random hex keys are the
    expected input.
    """
    entries: list[tuple[str, tuple[str, ...]]] = []
    for chunk in raw.split(";"):
        if not chunk.strip():
            continue
        if ":" not in chunk:
            raise ValueError(
                "CITE_KEY_ACLS entries must look like key:collection[,collection]; "
                f"got {chunk!r}"
            )
        key, raw_collections = chunk.split(":", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"CITE_KEY_ACLS has an empty key in {chunk!r}")
        if ":" in key:
            raise ValueError("CITE_KEY_ACLS keys cannot contain ':'")
        collections = []
        for item in raw_collections.split(","):
            item = item.strip()
            if not item:
                continue
            collections.append("*" if item == "*" else normalize_collection(item))
        entries.append((key, tuple(collections)))
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is calling, and what they may touch.

    ``collections is None`` means every collection. An empty frozenset means no
    collections, which is the correct default for an unlisted reader key.
    """

    identity: str
    role: Role = "reader"
    collections: frozenset[str] | None = frozenset()

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def can_read(self, collection: str) -> bool:
        return self.collections is None or collection in self.collections

    def require_admin(self) -> None:
        if not self.is_admin:
            raise PermissionError("admin role required")


@dataclass(frozen=True, slots=True)
class Settings:
    corpus_dir: Path
    index_path: Path
    golden_path: Path
    collections_dir: Path = Path("data/collections")
    default_collection: str = "default"
    api_keys: tuple[str, ...] = ()
    admin_keys: tuple[str, ...] = ()
    key_acls: tuple[tuple[str, tuple[str, ...]], ...] = field(default=())
    rate_limit_per_minute: int = 60
    rate_limit_backend: str = "sqlite"
    rate_limit_db_path: Path = Path("data/rate-limit.db")
    usage_db_path: Path = Path("data/usage.db")
    log_questions: bool = False
    judge: str = "lexical"
    pdf_ocr: str = "auto"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            corpus_dir=Path(os.getenv("CITE_CORPUS_DIR", "data/corpus")),
            index_path=Path(os.getenv("CITE_INDEX_PATH", "data/index.sqlite3")),
            golden_path=Path(os.getenv("CITE_GOLDEN_PATH", "eval/golden.yaml")),
            collections_dir=Path(os.getenv("CITE_COLLECTIONS_DIR", "data/collections")),
            default_collection=normalize_collection(
                os.getenv("CITE_COLLECTION", "default")
            ),
            api_keys=_split_keys(os.getenv("CITE_API_KEYS", "")),
            admin_keys=_split_keys(os.getenv("CITE_ADMIN_KEYS", "")),
            key_acls=parse_key_acls(os.getenv("CITE_KEY_ACLS", "")),
            rate_limit_per_minute=_int_env("CITE_RATE_LIMIT_PER_MINUTE", 60),
            rate_limit_backend=_choice_env(
                "CITE_RATE_LIMIT_BACKEND", "sqlite", {"sqlite", "memory"}
            ),
            rate_limit_db_path=Path(
                os.getenv("CITE_RATE_LIMIT_DB", "data/rate-limit.db")
            ),
            usage_db_path=Path(os.getenv("CITE_USAGE_DB", "data/usage.db")),
            log_questions=os.getenv("CITE_LOG_QUESTIONS", "").strip().lower() in _TRUE,
            judge=os.getenv("CITE_JUDGE", "lexical").strip().lower() or "lexical",
            pdf_ocr=_choice_env("CITE_PDF_OCR", "auto", {"auto", "always", "off"}),
        )

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_keys)

    @property
    def index_suffix(self) -> str:
        """Named collections follow the default index format.

        A deployment that keeps ``CITE_INDEX_PATH=data/index.json`` keeps JSON
        everywhere; the new SQLite default makes named collections ``.sqlite3``.
        """
        suffix = self.index_path.suffix.lower()
        return suffix if suffix in _INDEX_SUFFIXES else ".json"

    def index_path_for(self, collection: str | None = None) -> Path:
        """Default collection stays at ``CITE_INDEX_PATH``; named ones get a directory."""
        name = normalize_collection(collection, self.default_collection)
        if name == self.default_collection:
            return self.index_path
        return self.collections_dir / f"{name}{self.index_suffix}"

    def collection_paths(self) -> list[Path]:
        """Every index file on disk, one path per collection name.

        When a name has both a legacy ``.json`` and a migrated ``.sqlite3`` file,
        the configured default suffix wins instead of whichever sorts first.
        """
        paths: dict[str, Path] = {}
        if self.collections_dir.exists():
            for path in sorted(self.collections_dir.iterdir()):
                if not path.is_file() or path.suffix.lower() not in _INDEX_SUFFIXES:
                    continue
                current = paths.get(path.stem)
                if current is None or path.suffix.lower() == self.index_suffix:
                    paths[path.stem] = path
        return [paths[name] for name in sorted(paths)]

    def collection_names(self) -> list[str]:
        """Collections that have an index file on disk, default first."""
        names = [self.default_collection]
        for path in self.collection_paths():
            if path.stem not in names:
                names.append(path.stem)
        return names

    def principal(self, key: str | None, identity: str) -> Principal:
        """Resolve a validated key to a role and collection grants.

        Auth is disabled: the local caller is an admin, because there is no
        boundary to protect. Auth enabled with no ACL map: every valid key keeps
        the pre-ACL behaviour. Auth enabled with an ACL or admin map: least
        privilege, with unlisted keys getting no collections and no admin rights.
        """
        if not self.auth_enabled:
            return Principal(identity=identity, role="admin", collections=None)
        if key and any(_same(key, candidate) for candidate in self.admin_keys):
            return Principal(identity=identity, role="admin", collections=None)

        restricted = bool(self.admin_keys or self.key_acls)
        if not restricted:
            return Principal(identity=identity, role="reader", collections=None)

        for candidate, collections in self.key_acls:
            if _same(key or "", candidate):
                if "*" in collections:
                    return Principal(identity=identity, role="reader", collections=None)
                return Principal(
                    identity=identity,
                    role="reader",
                    collections=frozenset(collections),
                )
        return Principal(identity=identity, role="reader", collections=frozenset())


def _same(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
