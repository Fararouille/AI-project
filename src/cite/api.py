"""HTTP API and web UI host.

The default deployment is one process holding a small map of collection indexes.
That is the right amount of infrastructure for a single-node document QA service.
The hardening here is deliberately bounded:

* optional API keys (``CITE_API_KEYS``) on every endpoint that can spend money
* per-key collection ACLs and an admin role (``CITE_KEY_ACLS`` / ``CITE_ADMIN_KEYS``)
* token buckets shared by all workers on a host through SQLite by default
* a SQLite query log with an aggregate ``/api/usage`` endpoint
* named collections stored as separate index files, so one corpus cannot leak
  into another through a shared id space
* SQLite or JSON index storage, chosen by the configured file suffix
* admin-only browser uploads, stored outside the read-only corpus and layered
  back in on every rebuild, plus per-document scoping of ``/api/ask``

What is still missing for real multi-tenancy is listed in the README: per-tenant
embedding budgets, a shared vector server, and a reindex queue.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .answer import answer_question
from .config import Principal, Settings, normalize_collection
from .embeddings import Embedder, get_embedder
from .evaluation import (
    load_golden,
    render_answer_markdown,
    render_markdown,
    run_ablations,
    run_answer_eval,
)
from .ingest import build_index, load_index, save_index
from .judges import get_judge
from .models import Answer, ConfigResult
from .providers import get_provider
from .ratelimit import Limiter, build_limiter
from .retrieval import Index
from .uploads import UploadError, stage_removal, store_upload, stored_documents

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"


# --------------------------------------------------------------------------- usage store
class UsageStore:
    """Append-only SQLite log. Aggregates are computed on read at this scale."""

    def __init__(self, path: str | Path, log_questions: bool = False) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.log_questions = log_questions
        self._conn = sqlite3.connect(str(destination), check_same_thread=False, timeout=5.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                identity TEXT NOT NULL,
                collection TEXT NOT NULL,
                question_hash TEXT NOT NULL,
                question TEXT,
                question_chars INTEGER NOT NULL,
                mode TEXT NOT NULL,
                provider TEXT NOT NULL,
                latency_ms REAL NOT NULL,
                tokens_in INTEGER NOT NULL,
                tokens_out INTEGER NOT NULL,
                cost_usd REAL NOT NULL,
                citations INTEGER NOT NULL
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_queries_ts ON queries(ts)")
        self._conn.commit()
        self._lock = threading.Lock()

    def record(
        self,
        *,
        identity: str,
        collection: str,
        question: str,
        mode: str,
        provider: str,
        latency_ms: float,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        citations: int,
    ) -> None:
        digest = hashlib.sha256(question.encode("utf-8")).hexdigest()[:16]
        stored = question if self.log_questions else None
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO queries (
                    ts, identity, collection, question_hash, question, question_chars,
                    mode, provider, latency_ms, tokens_in, tokens_out, cost_usd, citations
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    identity,
                    collection,
                    digest,
                    stored,
                    len(question),
                    mode,
                    provider,
                    latency_ms,
                    tokens_in,
                    tokens_out,
                    cost_usd,
                    citations,
                ),
            )
            self._conn.commit()

    @staticmethod
    def _percentile(values: list[float], fraction: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        position = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
        return ordered[position]

    def summary(self, hours: int = 24) -> dict:
        cutoff = time.time() - max(1, hours) * 3600
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT ts, collection, mode, latency_ms, tokens_in, tokens_out,
                       cost_usd, citations
                FROM queries WHERE ts >= ? ORDER BY ts
                """,
                (cutoff,),
            ).fetchall()

        by_collection: dict[str, list[float]] = {}
        by_mode: dict[str, int] = {}
        for _, collection, mode, latency, *_ in rows:
            by_collection.setdefault(collection, []).append(latency)
            by_mode[mode] = by_mode.get(mode, 0) + 1

        latencies = [row[3] for row in rows]
        return {
            "window_hours": hours,
            "requests": len(rows),
            "avg_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
            "p95_latency_ms": round(self._percentile(latencies, 0.95), 2),
            "tokens_in": sum(row[4] for row in rows),
            "tokens_out": sum(row[5] for row in rows),
            "cost_usd": round(sum(row[6] for row in rows), 6),
            "citations": sum(row[7] for row in rows),
            "by_collection": [
                {
                    "collection": name,
                    "requests": len(values),
                    "avg_latency_ms": round(sum(values) / len(values), 2),
                }
                for name, values in sorted(by_collection.items())
            ],
            "by_mode": [
                {"mode": mode, "requests": count}
                for mode, count in sorted(by_mode.items())
            ],
            "last_request_at": (
                datetime.fromtimestamp(rows[-1][0], timezone.utc).isoformat()
                if rows
                else None
            ),
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --------------------------------------------------------------------------- app state
@dataclass
class AppState:
    index: Index | None = None
    collections: dict[str, Index] = field(default_factory=dict)
    last_eval: list[ConfigResult] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    settings: Settings | None = None
    limiter: Limiter | None = None
    limiter_key: tuple[int, str, str] | None = None
    usage: UsageStore | None = None
    default_collection: str = "default"


state = AppState()


def _settings() -> Settings:
    return state.settings or Settings.from_env()


def _configured_embedder() -> Embedder | None:
    explicit = os.getenv("CITE_EMBEDDER")
    return get_embedder(explicit) if explicit else None


def _collection(name: str | None) -> str:
    settings = _settings()
    try:
        return normalize_collection(name, settings.default_collection)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _migrate_legacy_json(settings: Settings) -> Path | None:
    """Move a pre-SQLite ``index.json`` to the configured path once.

    The default store changed from JSON to SQLite. Rebuilding would re-embed the
    corpus, which is free with the hashing embedder and expensive with a hosted
    one, so an existing sibling JSON index is loaded and written to the new path.
    """
    legacy = settings.index_path.with_suffix(".json")
    if (
        legacy == settings.index_path
        or not legacy.exists()
        or settings.index_path.suffix.lower() == ".json"
    ):
        return None
    try:
        index = load_index(legacy, _configured_embedder())
        save_index(index, settings.index_path)
    except Exception as exc:  # a bad legacy file must not block startup
        print(f"[cite] could not migrate {legacy}: {exc}")
        return None
    print(f"[cite] migrated {legacy} -> {settings.index_path}")
    return settings.index_path


def load_or_build_indexes() -> dict[str, Index]:
    settings = _settings()
    state.settings = settings
    state.default_collection = settings.default_collection
    indexes: dict[str, Index] = {}

    if not settings.index_path.exists():
        _migrate_legacy_json(settings)
    if settings.index_path.exists():
        indexes[settings.default_collection] = load_index(
            settings.index_path, _configured_embedder()
        )
    elif settings.corpus_dir.exists():
        index = build_index(
            settings.corpus_dir,
            embedder=_configured_embedder() or get_embedder(),
            collection=settings.default_collection,
            extra_roots=(settings.uploads_path_for(settings.default_collection),),
        )
        save_index(index, settings.index_path)
        indexes[settings.default_collection] = index

    for path in settings.collection_paths():
        name = path.stem
        if name in indexes or name == settings.default_collection:
            continue
        try:
            indexes[name] = load_index(path, _configured_embedder())
        except Exception as exc:  # one bad collection must not hide the rest
            print(f"[cite] skipped collection {name!r}: {exc}")

    state.collections = indexes
    state.index = indexes.get(settings.default_collection)
    return indexes


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = _settings()
    try:
        load_or_build_indexes()
    except Exception as exc:  # surfaced through /api/health rather than crashing the server
        print(f"[cite] index not loaded: {exc}")
    try:
        state.usage = UsageStore(settings.usage_db_path, settings.log_questions)
    except (sqlite3.Error, OSError) as exc:
        print(f"[cite] usage log disabled: {exc}")
        state.usage = None
    yield
    if state.usage is not None:
        state.usage.close()
    state.usage = None
    state.index = None
    state.collections = {}


app = FastAPI(
    title="cite",
    version=__version__,
    summary="Grounded answers with measurable retrieval.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- auth
def _presented_key(request: Request) -> str | None:
    direct = request.headers.get("x-api-key")
    if direct:
        return direct.strip()
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _authenticate(request: Request, settings: Settings) -> tuple[str | None, str]:
    """Return ``(presented_key, identity)`` or raise a 401."""
    key = _presented_key(request)
    if not settings.api_keys:
        host = request.client.host if request.client else "unknown"
        return None, f"ip:{host}"
    if not key:
        raise HTTPException(
            status_code=401,
            detail="missing API key; send X-API-Key or Authorization: Bearer <key>",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not any(
        hmac.compare_digest(key.encode("utf-8"), candidate.encode("utf-8"))
        for candidate in settings.api_keys
    ):
        raise HTTPException(
            status_code=401,
            detail="invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return key, "key:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def require_principal(request: Request) -> Principal:
    """Authenticate (when configured), resolve ACLs, and apply the token bucket.

    Auth is disabled when ``CITE_API_KEYS`` is empty, so the default offline
    install keeps working and the anonymous local caller is an admin. Rate
    limiting still applies by client IP in that mode.
    """
    settings = _settings()
    key, identity = _authenticate(request, settings)
    principal = settings.principal(key, identity)

    wanted = (
        settings.rate_limit_per_minute,
        settings.rate_limit_backend,
        str(settings.rate_limit_db_path),
    )
    if state.limiter is None or state.limiter_key != wanted:
        state.limiter = build_limiter(
            settings.rate_limit_per_minute,
            settings.rate_limit_backend,
            settings.rate_limit_db_path,
        )
        state.limiter_key = wanted

    allowed, retry_after = state.limiter.consume(identity)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )
    return principal


def require_identity(principal: Principal = Depends(require_principal)) -> str:
    """Backward-compatible dependency for callers that only need the identity."""
    return principal.identity


def require_admin(principal: Principal = Depends(require_principal)) -> Principal:
    if not principal.is_admin:
        raise HTTPException(
            status_code=403,
            detail="admin role required; use a key listed in CITE_ADMIN_KEYS",
        )
    return principal


def _usage_store(settings: Settings) -> UsageStore | None:
    if state.usage is not None:
        state.usage.log_questions = settings.log_questions
        return state.usage
    try:
        state.usage = UsageStore(settings.usage_db_path, settings.log_questions)
    except (sqlite3.Error, OSError) as exc:
        print(f"[cite] usage log disabled: {exc}")
        return None
    return state.usage


def _record(answer: Answer, collection: str, identity: str) -> None:
    store = _usage_store(_settings())
    if store is None:
        return
    try:
        store.record(
            identity=identity,
            collection=collection,
            question=answer.question,
            mode=answer.retrieval_mode,
            provider=answer.provider,
            latency_ms=answer.latency_ms,
            tokens_in=answer.tokens_in,
            tokens_out=answer.tokens_out,
            cost_usd=answer.cost_usd,
            citations=len(answer.citations),
        )
    except (sqlite3.Error, OSError) as exc:  # telemetry must never break an answer
        print(f"[cite] usage write failed: {exc}")


# --------------------------------------------------------------------------- schemas
class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=5, ge=1, le=20)
    mode: Literal["auto", "bm25", "dense", "hybrid"] = "auto"
    rerank: bool = False
    collection: str | None = None
    # Restrict retrieval to specific documents, e.g. the file just uploaded.
    doc_ids: list[str] | None = Field(default=None, max_length=50)


class CitationModel(BaseModel):
    marker: int
    doc_id: str
    title: str
    heading: str
    snippet: str
    score: float


class AskResponse(BaseModel):
    question: str
    answer: str
    collection: str
    citations: list[CitationModel]
    latency_ms: float
    mode: str
    reranked: bool
    provider: str
    cost_usd: float


class CollectionInfo(BaseModel):
    name: str
    documents: int
    chunks: int
    embedder: str


class HealthResponse(BaseModel):
    status: str
    version: str
    documents: int
    chunks: int
    embedder: str
    provider: str
    collections: list[CollectionInfo]
    auth_required: bool
    rate_limit_per_minute: int
    rate_limit_backend: str
    judge: str


class MeResponse(BaseModel):
    identity: str
    role: str
    collections: list[str] | None


class ReindexRequest(BaseModel):
    collection: str | None = None
    incremental: bool = True


class ReindexResponse(BaseModel):
    collection: str
    documents: int
    chunks: int
    stats: dict


class UploadResponse(BaseModel):
    collection: str
    doc_id: str
    title: str
    document_chunks: int
    replaced: bool
    documents: int
    chunks: int
    stats: dict


class DeleteResponse(BaseModel):
    collection: str
    doc_id: str
    documents: int
    chunks: int
    stats: dict


class UsageResponse(BaseModel):
    auth_required: bool
    summary: dict


# --------------------------------------------------------------------------- helpers
def _require_index(
    collection: str | None, principal: Principal
) -> tuple[str, Index]:
    name = _collection(collection)
    if not principal.can_read(name):
        raise HTTPException(
            status_code=403,
            detail=f"API key is not allowed to read collection {name!r}",
        )
    if name in state.collections:
        return name, state.collections[name]
    if state.index is not None and name == state.default_collection:
        return name, state.index
    if not state.collections and state.index is None and name == state.default_collection:
        raise HTTPException(status_code=503, detail="index not loaded; POST /api/reindex")
    available = sorted(state.collections) or [state.default_collection]
    raise HTTPException(
        status_code=404,
        detail=f"collection {name!r} is not loaded; available: {', '.join(available)}",
    )


def _resolve_scope(doc_ids: list[str] | None, index: Index) -> frozenset[str] | None:
    """Validate a per-question document filter against the loaded collection."""
    if not doc_ids:
        return None
    unique = sorted(set(doc_ids))
    known = set(index.manifest.get("documents") or {})
    if not known:
        known = {chunk.doc_id for chunk in index.chunks}
    missing = [doc_id for doc_id in unique if doc_id not in known]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"unknown document(s): {', '.join(missing)}",
        )
    return frozenset(unique)


def _rebuild_index(name: str, settings: Settings, *, incremental: bool = True) -> Index:
    """Rebuild one collection from its corpus plus uploads, then swap it in.

    The write lock covers the read-modify-write cycle, so two uploads arriving at
    once cannot both build from the same previous index and lose one document.
    """
    with state.lock:
        destination = settings.index_path_for(name)
        embedder = _configured_embedder() or get_embedder()

        previous = None
        if incremental and destination.exists():
            try:
                previous = load_index(destination, embedder, require_match=False)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print(f"[cite] previous index unusable ({exc}); rebuilding")

        index = build_index(
            settings.corpus_dir,
            embedder=embedder,
            previous=previous,
            incremental=incremental,
            collection=name,
            extra_roots=(settings.uploads_path_for(name),),
        )
        save_index(index, destination)
        state.collections[name] = index
        if name == settings.default_collection:
            state.index = index
        return index


def _serialize(answer: Answer, collection: str) -> AskResponse:
    return AskResponse(
        question=answer.question,
        answer=answer.text,
        collection=collection,
        citations=[
            CitationModel(
                marker=citation.marker,
                doc_id=citation.doc_id,
                title=citation.doc_title,
                heading=citation.heading,
                snippet=citation.snippet,
                score=citation.score,
            )
            for citation in answer.citations
        ],
        latency_ms=answer.latency_ms,
        mode=answer.retrieval_mode,
        reranked=answer.reranked,
        provider=answer.provider,
        cost_usd=answer.cost_usd,
    )


# --------------------------------------------------------------------------- routes
@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = _settings()
    default = state.index
    collections = [
        CollectionInfo(
            name=name,
            documents=len({chunk.doc_id for chunk in index.chunks}),
            chunks=len(index),
            embedder=index.embedder.name,
        )
        for name, index in sorted(state.collections.items())
    ]
    return HealthResponse(
        status="ok" if default is not None else "degraded",
        version=__version__,
        documents=len({chunk.doc_id for chunk in default.chunks}) if default else 0,
        chunks=len(default) if default else 0,
        embedder=default.embedder.name if default else "unavailable",
        provider=get_provider().name,
        collections=collections,
        auth_required=settings.auth_enabled,
        rate_limit_per_minute=settings.rate_limit_per_minute,
        rate_limit_backend=settings.rate_limit_backend,
        judge=settings.judge,
    )


@app.get("/api/me", response_model=MeResponse)
def me(principal: Principal = Depends(require_principal)) -> MeResponse:
    """Tell an authenticated client what its key can do.

    The bundled UI uses this to show a role badge instead of guessing from a 403.
    Collection names are only revealed when the key may read them.
    """
    return MeResponse(
        identity=principal.identity,
        role=principal.role,
        collections=(
            None
            if principal.collections is None
            else sorted(principal.collections & state.collections.keys())
        ),
    )


@app.get("/api/sources")
def sources(
    collection: str | None = None,
    principal: Principal = Depends(require_principal),
) -> list[dict]:
    name, index = _require_index(collection, principal)
    uploaded_ids = stored_documents(_settings(), name)
    grouped: dict[str, dict] = {}
    for chunk in index.chunks:
        entry = grouped.setdefault(
            chunk.doc_id,
            {
                "doc_id": chunk.doc_id,
                "title": chunk.doc_title,
                "collection": name,
                "chunks": 0,
                "headings": [],
                "uploaded": chunk.doc_id in uploaded_ids,
            },
        )
        entry["chunks"] += 1
        if chunk.heading and chunk.heading not in entry["headings"]:
            entry["headings"].append(chunk.heading)
    return sorted(grouped.values(), key=lambda item: item["title"])


@app.post("/api/ask", response_model=AskResponse)
def ask(
    request: AskRequest, principal: Principal = Depends(require_principal)
) -> AskResponse:
    name, index = _require_index(request.collection, principal)
    answer = answer_question(
        index,
        request.question,
        provider=get_provider(),
        k=request.k,
        mode=request.mode,
        rerank=request.rerank,
        doc_ids=_resolve_scope(request.doc_ids, index),
    )
    _record(answer, name, principal.identity)
    return _serialize(answer, name)


@app.post("/api/ask/stream")
async def ask_stream(
    request: AskRequest, principal: Principal = Depends(require_principal)
) -> StreamingResponse:
    name, index = _require_index(request.collection, principal)
    answer = answer_question(
        index,
        request.question,
        provider=get_provider(),
        k=request.k,
        mode=request.mode,
        rerank=request.rerank,
        doc_ids=_resolve_scope(request.doc_ids, index),
    )
    _record(answer, name, principal.identity)

    async def events():
        # Tokens are chunked from a completed answer: the local provider is not a
        # streaming model. Hosted providers can later push deltas here unchanged.
        for token in answer.text.split(" "):
            payload = json.dumps({"delta": token + " "})
            yield f"event: token\ndata: {payload}\n\n"
            await asyncio.sleep(0.012)
        payload = _serialize(answer, name).model_dump_json()
        yield f"event: done\ndata: {payload}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/api/eval")
def evaluate(
    answers: bool = False,
    collection: str | None = None,
    judge: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict:
    _, index = _require_index(collection, principal)
    settings = _settings()
    try:
        golden = load_golden(settings.golden_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    with state.lock:
        results = run_ablations(index, golden.cases, k=golden.k)
        state.last_eval = results
    payload = {
        "markdown": render_markdown(results, index.embedder.name),
        "queries": len(golden.cases),
        "results": [
            {
                "name": item.name,
                "recall_at_k": round(item.recall_at_k, 4),
                "mrr": round(item.mrr, 4),
                "ndcg_at_k": round(item.ndcg_at_k, 4),
                "latency_p50_ms": round(item.latency_p50_ms, 2),
                "latency_p95_ms": round(item.latency_p95_ms, 2),
            }
            for item in results
        ],
    }
    if answers:
        try:
            support_judge = get_judge(judge or settings.judge)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        answer_result = run_answer_eval(
            index,
            golden.cases,
            provider=get_provider(),
            k=golden.k,
            mode="auto",
            judge=support_judge,
        )
        payload["answer_markdown"] = render_answer_markdown([answer_result])
        payload["answers"] = {
            "judge": answer_result.judge,
            "answered_rate": round(answer_result.answered_rate, 4),
            "grounded_rate": round(answer_result.grounded_rate, 4),
            "citation_precision": round(answer_result.citation_precision, 4),
            "citation_recall": round(answer_result.citation_recall, 4),
            "support_rate": round(answer_result.support_rate, 4),
            "unsupported_claim_rate": round(answer_result.unsupported_claim_rate, 4),
            "fact_recall": round(answer_result.fact_recall, 4),
        }
    return payload


@app.post("/api/reindex", response_model=ReindexResponse)
def reindex(
    request: ReindexRequest | None = None,
    principal: Principal = Depends(require_admin),
) -> ReindexResponse:
    settings = _settings()
    options = request or ReindexRequest()
    name = _collection(options.collection)
    if not principal.can_read(name):
        raise HTTPException(
            status_code=403,
            detail=f"API key is not allowed to read collection {name!r}",
        )
    try:
        index = _rebuild_index(name, settings, incremental=options.incremental)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ReindexResponse(
        collection=name,
        documents=len(index.manifest.get("documents", {})),
        chunks=len(index),
        stats=index.stats.as_dict() if index.stats else {},
    )


@app.post("/api/documents", response_model=UploadResponse, status_code=201)
def upload_document(
    file: UploadFile = File(...),
    principal: Principal = Depends(require_admin),
) -> UploadResponse:
    """Index one file from the browser into the default collection.

    Uploads land in ``CITE_UPLOADS_DIR`` rather than the corpus directory, so the
    corpus stays read-only and the uploaded file is still there after a restart.
    Every rebuild layers uploads over the corpus, reusing unchanged chunks.
    """
    settings = _settings()
    if settings.max_upload_mb <= 0:
        raise HTTPException(
            status_code=413,
            detail="uploads are disabled; set CITE_MAX_UPLOAD_MB above zero",
        )
    limit = settings.max_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds CITE_MAX_UPLOAD_MB ({settings.max_upload_mb} MB)",
        )

    name = settings.default_collection
    try:
        stored = store_upload(settings, name, file.filename, data)
    except UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    previous = state.collections.get(name) or state.index
    replaced = stored.doc_id in (previous.manifest.get("documents", {}) if previous else {})

    try:
        index = _rebuild_index(name, settings)
    except (FileNotFoundError, ValueError) as exc:
        stored.restore()
        raise HTTPException(status_code=400, detail=f"could not index upload: {exc}") from exc
    except Exception:
        stored.restore()
        raise

    return UploadResponse(
        collection=name,
        doc_id=stored.doc_id,
        title=stored.title,
        document_chunks=sum(1 for chunk in index.chunks if chunk.doc_id == stored.doc_id),
        replaced=replaced,
        documents=len(index.manifest.get("documents", {})),
        chunks=len(index),
        stats=index.stats.as_dict() if index.stats else {},
    )


@app.delete("/api/documents/{doc_id}", response_model=DeleteResponse)
def delete_document(
    doc_id: str,
    principal: Principal = Depends(require_admin),
) -> DeleteResponse:
    """Remove an uploaded document and rebuild the collection without it.

    Corpus files are never deleted through the API. If an upload was shadowing a
    corpus document with the same id, the corpus version comes back on rebuild.
    """
    settings = _settings()
    name = settings.default_collection
    try:
        staged = stage_removal(settings, name, doc_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        index = _rebuild_index(name, settings)
    except (FileNotFoundError, ValueError) as exc:
        staged.restore()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        staged.restore()
        raise
    staged.discard()

    return DeleteResponse(
        collection=name,
        doc_id=doc_id,
        documents=len(index.manifest.get("documents", {})),
        chunks=len(index),
        stats=index.stats.as_dict() if index.stats else {},
    )


@app.get("/api/usage", response_model=UsageResponse)
def usage(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    principal: Principal = Depends(require_admin),
) -> UsageResponse:
    settings = _settings()
    store = _usage_store(settings)
    if store is None:
        raise HTTPException(status_code=503, detail="usage log unavailable")
    return UsageResponse(
        auth_required=settings.auth_enabled,
        summary=store.summary(hours=hours),
    )


# --------------------------------------------------------------------------- static UI
if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")

    @app.get("/")
    def ui() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

else:

    @app.get("/")
    def missing_ui() -> JSONResponse:
        return JSONResponse(
            {
                "service": "cite",
                "version": __version__,
                "hint": "frontend not built; run `npm install && npm run build` in frontend/",
                "docs": "/docs",
            }
        )
