from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cite import api as api_module
from cite.config import Settings
from cite.embeddings import HashingEmbedder
from cite.models import Chunk
from cite.retrieval import Index


@pytest.fixture()
def client(monkeypatch, tmp_path):
    chunks = [
        Chunk(
            chunk_id="c1",
            doc_id="rate-limits",
            doc_title="Rate Limits",
            heading="Limits",
            ordinal=0,
            body="Event ingestion is limited to 600 requests per minute per project.",
            text="Rate Limits\nLimits\nEvent ingestion is limited to 600 requests per minute.",
        )
    ]
    api_module.state.index = Index(chunks, HashingEmbedder(dim=64))
    api_module.state.collections = {"default": api_module.state.index}
    api_module.state.settings = Settings(
        corpus_dir=tmp_path / "corpus",
        index_path=tmp_path / "index.json",
        golden_path=tmp_path / "golden.yaml",
        collections_dir=tmp_path / "collections",
        usage_db_path=tmp_path / "usage.db",
        rate_limit_per_minute=0,
    )
    api_module.state.limiter = None
    api_module.state.usage = None
    monkeypatch.setattr(api_module, "FRONTEND_DIR", tmp_path / "missing")
    yield TestClient(api_module.app)
    if api_module.state.usage is not None:
        api_module.state.usage.close()
    api_module.state.index = None
    api_module.state.collections = {}
    api_module.state.settings = None
    api_module.state.usage = None


def test_health_reports_loaded_index(client) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["chunks"] == 1
    assert body["documents"] == 1


def test_ask_returns_answer_with_citations(client) -> None:
    response = client.post("/api/ask", json={"question": "How many requests per minute?"})
    assert response.status_code == 200
    body = response.json()
    assert body["answer"]
    assert body["citations"][0]["doc_id"] == "rate-limits"
    assert body["mode"] == "bm25"


def test_ask_validates_question(client) -> None:
    assert client.post("/api/ask", json={"question": ""}).status_code == 422
    assert client.post("/api/ask", json={"question": "ok", "k": 0}).status_code == 422


def test_sources_lists_documents(client) -> None:
    body = client.get("/api/sources").json()
    assert body[0]["doc_id"] == "rate-limits"
    assert body[0]["chunks"] == 1


def test_stream_emits_tokens_then_done(client) -> None:
    with client.stream(
        "POST", "/api/ask/stream", json={"question": "How many requests per minute?"}
    ) as response:
        body = "".join(response.iter_text())
    assert "event: token" in body
    assert "event: done" in body
    assert "citations" in body


def test_ask_returns_503_without_index(client) -> None:
    api_module.state.index = None
    api_module.state.collections = {}
    response = client.post("/api/ask", json={"question": "anything"})
    assert response.status_code == 503
