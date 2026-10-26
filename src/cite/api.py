"""HTTP API and web UI host.

The index is loaded once at startup and held in a small lock-protected store. That
is the right amount of infrastructure for a single-node document QA service; the
README explains where it would need to change for multi-tenant use.
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .answer import answer_question
from .config import Settings
from .embeddings import get_embedder
from .evaluation import load_golden, render_markdown, run_ablations
from .ingest import build_index, load_index
from .models import Answer, ConfigResult
from .providers import get_provider
from .retrieval import Index

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@dataclass
class AppState:
    index: Index | None = None
    last_eval: list[ConfigResult] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


state = AppState()


def load_or_build_index() -> Index:
    settings = Settings.from_env()
    embedder = get_embedder()
    if settings.index_path.exists():
        return load_index(settings.index_path, embedder)
    if settings.corpus_dir.exists():
        index = build_index(settings.corpus_dir, embedder=embedder)
        settings.index_path.parent.mkdir(parents=True, exist_ok=True)
        from .ingest import save_index

        save_index(index, settings.index_path)
        return index
    raise RuntimeError(
        "no index found; run `cite ingest --corpus data/corpus` or set CITE_INDEX_PATH"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        state.index = load_or_build_index()
    except Exception as exc:  # surfaced through /api/health rather than crashing the server
        print(f"[cite] index not loaded: {exc}")
    yield
    state.index = None


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


# --------------------------------------------------------------------------- schemas
class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=5, ge=1, le=20)
    mode: Literal["auto", "bm25", "dense", "hybrid"] = "auto"
    rerank: bool = False


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
    citations: list[CitationModel]
    latency_ms: float
    mode: str
    reranked: bool
    provider: str
    cost_usd: float


class HealthResponse(BaseModel):
    status: str
    version: str
    documents: int
    chunks: int
    embedder: str
    provider: str


class ReindexResponse(BaseModel):
    documents: int
    chunks: int


def _require_index() -> Index:
    if state.index is None:
        raise HTTPException(status_code=503, detail="index not loaded; POST /api/reindex")
    return state.index


def _serialize(answer: Answer) -> AskResponse:
    return AskResponse(
        question=answer.question,
        answer=answer.text,
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
    index = state.index
    return HealthResponse(
        status="ok" if index is not None else "degraded",
        version=__version__,
        documents=len({chunk.doc_id for chunk in index.chunks}) if index else 0,
        chunks=len(index) if index else 0,
        embedder=index.embedder.name if index else "unavailable",
        provider=get_provider().name,
    )


@app.get("/api/sources")
def sources() -> list[dict]:
    index = _require_index()
    grouped: dict[str, dict] = {}
    for chunk in index.chunks:
        entry = grouped.setdefault(
            chunk.doc_id,
            {"doc_id": chunk.doc_id, "title": chunk.doc_title, "chunks": 0, "headings": []},
        )
        entry["chunks"] += 1
        if chunk.heading and chunk.heading not in entry["headings"]:
            entry["headings"].append(chunk.heading)
    return sorted(grouped.values(), key=lambda item: item["title"])


@app.post("/api/ask", response_model=AskResponse)
def ask(request: AskRequest) -> AskResponse:
    index = _require_index()
    answer = answer_question(
        index,
        request.question,
        provider=get_provider(),
        k=request.k,
        mode=request.mode,
        rerank=request.rerank,
    )
    return _serialize(answer)


@app.post("/api/ask/stream")
async def ask_stream(request: AskRequest) -> StreamingResponse:
    index = _require_index()
    answer = answer_question(
        index,
        request.question,
        provider=get_provider(),
        k=request.k,
        mode=request.mode,
        rerank=request.rerank,
    )

    async def events():
        # Tokens are chunked from a completed answer: the local provider is not a
        # streaming model. Hosted providers can later push deltas here unchanged.
        for token in answer.text.split(" "):
            payload = json.dumps({"delta": token + " "})
            yield f"event: token\ndata: {payload}\n\n"
            await asyncio.sleep(0.012)
        payload = _serialize(answer).model_dump_json()
        yield f"event: done\ndata: {payload}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/api/eval")
def evaluate() -> dict:
    index = _require_index()
    settings = Settings.from_env()
    try:
        golden = load_golden(settings.golden_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    with state.lock:
        results = run_ablations(index, golden.cases, k=golden.k)
        state.last_eval = results
    return {
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


@app.post("/api/reindex", response_model=ReindexResponse)
def reindex() -> ReindexResponse:
    settings = Settings.from_env()
    try:
        index = build_index(settings.corpus_dir, embedder=get_embedder())
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with state.lock:
        state.index = index
    return ReindexResponse(
        documents=len({chunk.doc_id for chunk in index.chunks}), chunks=len(index)
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
