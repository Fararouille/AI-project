from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from cite import api as api_module
from cite.api import UsageStore
from cite.config import Settings
from cite.embeddings import HashingEmbedder
from cite.models import Chunk
from cite.retrieval import Index


def make_index(doc_id: str = "rate-limits", body: str | None = None) -> Index:
    body = body or "Event ingestion is limited to 600 requests per minute."
    chunk = Chunk(
        chunk_id=f"{doc_id}-c1",
        doc_id=doc_id,
        doc_title=doc_id.replace("-", " ").title(),
        heading="Limits",
        ordinal=0,
        body=body,
        text=f"{doc_id}\nLimits\n{body}",
    )
    return Index([chunk], HashingEmbedder(dim=64))


@pytest.fixture()
def configured(monkeypatch, tmp_path):
    settings = Settings(
        corpus_dir=tmp_path / "corpus",
        index_path=tmp_path / "index.json",
        golden_path=tmp_path / "golden.yaml",
        collections_dir=tmp_path / "collections",
        usage_db_path=tmp_path / "usage.db",
        rate_limit_per_minute=0,
    )
    monkeypatch.setattr(api_module.state, "settings", settings)
    api_module.state.index = make_index()
    api_module.state.collections = {"default": api_module.state.index}
    api_module.state.limiter = None
    api_module.state.usage = None
    client = TestClient(api_module.app)
    yield client, settings
    if api_module.state.usage is not None:
        api_module.state.usage.close()
    api_module.state.index = None
    api_module.state.collections = {}
    api_module.state.limiter = None
    api_module.state.usage = None


def ask(client: TestClient, **overrides):
    payload = {"question": "How many requests per minute?"}
    payload.update(overrides)
    return client.post("/api/ask", json=payload)


def test_auth_disabled_allows_anonymous(configured) -> None:
    client, _ = configured
    assert ask(client).status_code == 200
    health = client.get("/api/health").json()
    assert health["auth_required"] is False


def test_missing_key_is_rejected(configured) -> None:
    client, settings = configured
    api_module.state.settings = replace(settings, api_keys=("secret",))
    response = ask(client)
    assert response.status_code == 401
    assert "x-api-key" in response.json()["detail"].lower()
    assert response.headers["www-authenticate"] == "Bearer"


def test_x_api_key_and_bearer_are_accepted(configured) -> None:
    client, settings = configured
    api_module.state.settings = replace(settings, api_keys=("secret",))
    assert ask(client).status_code == 401
    assert ask(client).status_code == 401
    first = client.post(
        "/api/ask",
        json={"question": "How many requests per minute?"},
        headers={"X-API-Key": "secret"},
    )
    second = client.post(
        "/api/ask",
        json={"question": "How many requests per minute?"},
        headers={"Authorization": "Bearer secret"},
    )
    assert first.status_code == 200
    assert second.status_code == 200


def test_wrong_key_is_rejected(configured) -> None:
    client, settings = configured
    api_module.state.settings = replace(settings, api_keys=("secret",))
    response = client.post(
        "/api/ask",
        json={"question": "anything"},
        headers={"X-API-Key": "not-the-key"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid API key"


def test_rate_limit_returns_429_and_retry_after(configured) -> None:
    client, settings = configured
    api_module.state.settings = replace(settings, rate_limit_per_minute=1)
    api_module.state.limiter = None
    assert ask(client).status_code == 200
    response = ask(client)
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) >= 1
    assert response.json()["detail"] == "rate limit exceeded"


def test_health_is_public_even_when_auth_is_on(configured) -> None:
    client, settings = configured
    api_module.state.settings = replace(settings, api_keys=("secret",))
    health = client.get("/api/health").json()
    assert health["auth_required"] is True
    assert health["collections"][0]["name"] == "default"


def test_usage_endpoint_aggregates_requests(configured) -> None:
    client, _ = configured
    assert ask(client).status_code == 200
    body = client.get("/api/usage?hours=1").json()
    summary = body["summary"]
    assert summary["requests"] == 1
    assert summary["by_collection"][0]["collection"] == "default"
    assert summary["by_mode"][0]["mode"] == "bm25"
    assert summary["citations"] >= 0


def test_usage_endpoint_requires_auth_when_configured(configured) -> None:
    client, settings = configured
    api_module.state.settings = replace(settings, api_keys=("secret",))
    assert client.get("/api/usage").status_code == 401


def test_usage_log_redacts_questions_by_default(tmp_path) -> None:
    path = tmp_path / "usage.db"
    store = UsageStore(path, log_questions=False)
    store.record(
        identity="ip:test",
        collection="default",
        question="how many requests per minute?",
        mode="bm25",
        provider="mock",
        latency_ms=1.0,
        tokens_in=4,
        tokens_out=2,
        cost_usd=0.0,
        citations=1,
    )
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT question FROM queries").fetchone()[0] is None

    store.log_questions = True
    store.record(
        identity="ip:test",
        collection="default",
        question="how many requests per minute?",
        mode="bm25",
        provider="mock",
        latency_ms=1.0,
        tokens_in=4,
        tokens_out=2,
        cost_usd=0.0,
        citations=1,
    )
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT question FROM queries ORDER BY id").fetchall()
    assert rows[0][0] is None
    assert rows[1][0] == "how many requests per minute?"
    store.close()


def test_collection_scoping(configured) -> None:
    client, _ = configured
    api_module.state.collections["support"] = make_index(
        "support", "Support replies within four hours."
    )
    body = ask(
        client, collection="support", question="How fast does support reply?"
    ).json()
    assert body["collection"] == "support"
    assert body["citations"][0]["doc_id"] == "support"

    sources = client.get("/api/sources?collection=support").json()
    assert sources[0]["doc_id"] == "support"
    assert sources[0]["collection"] == "support"

    assert ask(client, collection="missing").status_code == 404
    assert ask(client, collection="Bad Name").status_code == 400


def test_reindex_uses_the_manifest_on_the_second_run(configured, tmp_path) -> None:
    client, settings = configured
    corpus = tmp_path / "reindex-corpus"
    corpus.mkdir()
    (corpus / "alpha.md").write_text(
        "# Alpha\n\nAlpha rate limit is 10 requests per minute.", encoding="utf-8"
    )
    index_path = tmp_path / "reindexed.json"
    api_module.state.settings = replace(
        settings, corpus_dir=corpus, index_path=index_path
    )

    first = client.post("/api/reindex", json={})
    assert first.status_code == 200
    first_stats = first.json()["stats"]
    assert first_stats["full_rebuild"] is True
    assert first_stats["chunks_embedded"] == first_stats["chunks"]

    second = client.post("/api/reindex", json={"incremental": True})
    assert second.status_code == 200
    second_stats = second.json()["stats"]
    assert second_stats["full_rebuild"] is False
    assert second_stats["chunks_reused"] == second_stats["chunks"]
    assert second_stats["chunks_embedded"] == 0


def test_reindex_reports_missing_corpus(configured) -> None:
    client, _ = configured
    response = client.post("/api/reindex", json={})
    assert response.status_code == 400


def test_sources_unknown_collection_is_404(configured) -> None:
    client, _ = configured
    response = client.get("/api/sources?collection=nope")
    assert response.status_code == 404
    assert "available" in response.json()["detail"]
