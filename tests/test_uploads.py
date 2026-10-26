from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from cite import api as api_module
from cite.config import Settings
from cite.embeddings import HashingEmbedder
from cite.ingest import build_index, save_index


def upload_file(name: str, body: bytes, content_type: str = "text/markdown") -> dict:
    return {"file": (name, body, content_type)}


@pytest.fixture()
def configured(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "handbook.md").write_text(
        "# Handbook\n\nWidget limit is 10 per account.\n", encoding="utf-8"
    )
    settings = Settings(
        corpus_dir=corpus,
        index_path=tmp_path / "index.sqlite3",
        golden_path=tmp_path / "golden.yaml",
        collections_dir=tmp_path / "collections",
        usage_db_path=tmp_path / "usage.db",
        uploads_dir=tmp_path / "uploads",
        max_upload_mb=1,
        rate_limit_per_minute=0,
        rate_limit_backend="memory",
        rate_limit_db_path=tmp_path / "rate-limit.db",
    )
    monkeypatch.setattr(api_module.state, "settings", settings)
    monkeypatch.delenv("CITE_EMBEDDER", raising=False)
    monkeypatch.setattr(api_module, "FRONTEND_DIR", tmp_path / "missing")

    index = build_index(corpus, embedder=HashingEmbedder())
    save_index(index, settings.index_path)
    api_module.state.index = index
    api_module.state.collections = {"default": index}
    api_module.state.limiter = None
    api_module.state.usage = None

    yield TestClient(api_module.app), settings

    if api_module.state.usage is not None:
        api_module.state.usage.close()
    api_module.state.index = None
    api_module.state.collections = {}
    api_module.state.settings = None
    api_module.state.limiter = None
    api_module.state.usage = None


def test_upload_indexes_a_file_and_stores_it_outside_the_corpus(configured) -> None:
    client, settings = configured
    response = client.post(
        "/api/documents",
        files=upload_file("widgets.md", b"# Widgets\n\nThe widget limit is 250 per day.\n"),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["doc_id"] == "widgets"
    assert body["title"] == "Widgets"
    assert body["document_chunks"] == 1
    assert body["replaced"] is False
    assert body["documents"] == 2
    assert (settings.uploads_dir / "default" / "widgets.md").exists()
    assert not (settings.corpus_dir / "widgets.md").exists()

    listed = {source["doc_id"]: source for source in client.get("/api/sources").json()}
    assert listed["widgets"]["uploaded"] is True
    assert listed["handbook"]["uploaded"] is False


def test_question_can_be_scoped_to_the_uploaded_document(configured) -> None:
    client, _ = configured
    client.post(
        "/api/documents",
        files=upload_file("widgets.md", b"# Widgets\n\nThe widget limit is 250 per day.\n"),
    )

    scoped = client.post(
        "/api/ask",
        json={"question": "What is the widget limit?", "doc_ids": ["widgets"]},
    )
    assert scoped.status_code == 200
    citations = scoped.json()["citations"]
    assert citations
    assert {citation["doc_id"] for citation in citations} == {"widgets"}
    assert "250" in citations[0]["snippet"]

    # The same question without a scope may answer from either document.
    unscoped = client.post("/api/ask", json={"question": "What is the widget limit?"})
    assert unscoped.status_code == 200


def test_unknown_document_scope_is_a_404(configured) -> None:
    client, _ = configured
    response = client.post(
        "/api/ask", json={"question": "anything", "doc_ids": ["missing"]}
    )
    assert response.status_code == 404
    assert "unknown document" in response.json()["detail"]


def test_reindex_keeps_uploads(configured) -> None:
    client, _ = configured
    client.post(
        "/api/documents",
        files=upload_file("widgets.md", b"# Widgets\n\nThe widget limit is 250 per day.\n"),
    )

    assert client.post("/api/reindex", json={}).status_code == 200

    sources = {source["doc_id"]: source for source in client.get("/api/sources").json()}
    assert sources["widgets"]["uploaded"] is True
    scoped = client.post(
        "/api/ask", json={"question": "widget limit", "doc_ids": ["widgets"]}
    )
    assert scoped.json()["citations"][0]["doc_id"] == "widgets"


def test_upload_shadows_and_delete_restores_a_corpus_document(configured) -> None:
    client, _ = configured
    replaced = client.post(
        "/api/documents",
        files=upload_file(
            "handbook.md", b"# Handbook\n\nWidget limit is 999 per account.\n"
        ),
    )
    assert replaced.status_code == 201
    assert replaced.json()["replaced"] is True

    answer = client.post(
        "/api/ask", json={"question": "What is the widget limit?", "doc_ids": ["handbook"]}
    )
    assert "999" in answer.json()["citations"][0]["snippet"]

    removed = client.delete("/api/documents/handbook")
    assert removed.status_code == 200
    assert removed.json()["documents"] == 1

    restored = client.post(
        "/api/ask", json={"question": "What is the widget limit?", "doc_ids": ["handbook"]}
    )
    assert "10 per account" in restored.json()["citations"][0]["snippet"]


def test_delete_only_removes_uploads(configured) -> None:
    client, settings = configured
    client.post(
        "/api/documents",
        files=upload_file("widgets.md", b"# Widgets\n\nThe widget limit is 250 per day.\n"),
    )

    assert client.delete("/api/documents/widgets").status_code == 200
    assert not (settings.uploads_dir / "default" / "widgets.md").exists()
    assert client.get("/api/sources").json()[0]["doc_id"] == "handbook"

    assert client.delete("/api/documents/widgets").status_code == 404


def test_upload_without_a_heading_gets_a_clean_title(configured) -> None:
    client, settings = configured
    response = client.post(
        "/api/documents",
        files=upload_file("quarter-close.md", b"Refunds above 500 need approval.\n"),
    )

    assert response.status_code == 201
    assert response.json()["doc_id"] == "quarter-close"
    assert response.json()["title"] == "Quarter Close"
    # The staging directory is removed; only the final file is left behind.
    assert sorted(path.name for path in (settings.uploads_dir / "default").iterdir()) == [
        "quarter-close.md"
    ]
    staging = settings.uploads_dir / "_staging"
    assert not staging.exists() or not any(staging.iterdir())


def test_upload_strips_a_utf8_bom_before_reading_the_title(configured) -> None:
    client, _ = configured
    response = client.post(
        "/api/documents",
        files=upload_file("bom.md", "# Real Title\n\nwidget limit is 77\n".encode("utf-8-sig")),
    )

    assert response.status_code == 201
    assert response.json()["title"] == "Real Title"
    assert client.delete("/api/documents/handbook").status_code == 404


def test_reupload_with_another_extension_replaces_the_file(configured) -> None:
    client, settings = configured
    client.post(
        "/api/documents",
        files=upload_file("widgets.md", b"# Widgets\n\nThe widget limit is 250 per day.\n"),
    )
    response = client.post(
        "/api/documents",
        files=upload_file("widgets.txt", b"# Widgets\n\nThe widget limit is 300 per day.\n"),
    )

    assert response.status_code == 201
    assert response.json()["replaced"] is True
    directory = settings.uploads_dir / "default"
    assert sorted(path.name for path in directory.iterdir()) == ["widgets.txt"]


def test_upload_rejects_path_traversal_in_the_file_name(configured) -> None:
    client, settings = configured
    response = client.post(
        "/api/documents",
        files=upload_file("..\\..\\escape.md", b"# Escape\n\nnot outside the upload directory"),
    )

    assert response.status_code == 201
    assert response.json()["doc_id"] == "escape"
    assert (settings.uploads_dir / "default" / "escape.md").exists()
    assert not (settings.uploads_dir / "escape.md").exists()


def test_upload_rejects_unsupported_types_and_empty_files(configured) -> None:
    client, _ = configured
    wrong_type = client.post(
        "/api/documents", files=upload_file("notes.rtf", b"not markdown")
    )
    assert wrong_type.status_code == 400
    assert "unsupported file type" in wrong_type.json()["detail"]

    empty = client.post(
        "/api/documents", files=upload_file("empty.md", b"   \n\n", "text/markdown")
    )
    assert empty.status_code == 400
    assert "no extractable text" in empty.json()["detail"]


def test_upload_size_limit_returns_413(configured) -> None:
    client, _ = configured
    response = client.post(
        "/api/documents",
        files=upload_file("big.md", b"x" * (1024 * 1024 + 1)),
    )
    assert response.status_code == 413
    assert "CITE_MAX_UPLOAD_MB" in response.json()["detail"]


def test_upload_and_delete_require_an_admin_key(configured) -> None:
    client, settings = configured
    api_module.state.settings = replace(
        settings,
        api_keys=("reader-key", "admin-key"),
        admin_keys=("admin-key",),
        key_acls=(("reader-key", ("default",)), ("admin-key", ("*",))),
    )

    anonymous = client.post("/api/documents", files=upload_file("a.md", b"# A\n\nhello"))
    assert anonymous.status_code == 401
    reader = client.post(
        "/api/documents",
        files=upload_file("a.md", b"# A\n\nhello"),
        headers={"X-API-Key": "reader-key"},
    )
    assert reader.status_code == 403
    created = client.post(
        "/api/documents",
        files=upload_file("a.md", b"# A\n\nwidget limit is 42"),
        headers={"X-API-Key": "admin-key"},
    )
    assert created.status_code == 201

    denied = client.delete("/api/documents/a", headers={"X-API-Key": "reader-key"})
    assert denied.status_code == 403
    allowed = client.delete("/api/documents/a", headers={"X-API-Key": "admin-key"})
    assert allowed.status_code == 200
