from __future__ import annotations

from pathlib import Path

import pytest

from cite.ingest import load_documents
from cite.loaders import SUPPORTED_SUFFIXES, get_loader, load_document
from cite.loaders.html import HTMLLoader
from cite.loaders.markdown import MarkdownLoader
from cite.loaders.pdf import PDFLoader

FIXTURES = Path(__file__).parent / "fixtures"


def test_supported_suffixes_come_from_registry() -> None:
    assert {".md", ".markdown", ".txt", ".html", ".htm", ".pdf"} <= SUPPORTED_SUFFIXES
    assert isinstance(get_loader("notes.md"), MarkdownLoader)
    assert isinstance(get_loader("page.HTML"), HTMLLoader)
    assert isinstance(get_loader("paper.pdf"), PDFLoader)


def test_markdown_title_prefers_first_h1(tmp_path) -> None:
    path = tmp_path / "guide.md"
    path.write_text("intro\n\n# Real Title\n\nbody", encoding="utf-8")
    loaded = load_document(path)
    assert loaded.title == "Real Title"
    assert loaded.text.startswith("intro")


def test_markdown_title_falls_back_to_filename(tmp_path) -> None:
    path = tmp_path / "rate-limits.md"
    path.write_text("no heading here", encoding="utf-8")
    assert load_document(path).title == "Rate Limits"


def test_html_drops_navigation_scripts_and_footers() -> None:
    loaded = load_document(FIXTURES / "handbook.html")
    assert loaded.title == "Widget API"
    assert "do-not-index-this" not in loaded.text
    assert "Related links" not in loaded.text
    assert "Copyright" not in loaded.text


def test_html_keeps_headings_lists_tables_and_code() -> None:
    text = load_document(FIXTURES / "handbook.html").text
    assert "## Limits" in text
    assert "250 widgets per day" in text
    assert "| Plan | Price |\n|---|---|\n| Free | $0 |\n| Growth | $49 |" in text
    assert '"name": "signup"' in text


def test_html_without_blocks_falls_back_to_loose_text(tmp_path) -> None:
    path = tmp_path / "loose.html"
    path.write_text("<html><body>just text<br>and more</body></html>", encoding="utf-8")
    loaded = load_document(path)
    assert "just text" in loaded.text and "and more" in loaded.text


def test_pdf_extracts_pages_with_page_markers() -> None:
    loaded = load_document(FIXTURES / "handbook.pdf")
    assert "## Page 1" in loaded.text
    assert "Widgets are created with POST /v1/widgets." in loaded.text
    assert "250 widgets per day" in loaded.text


def test_unsupported_suffix_is_explicit(tmp_path) -> None:
    path = tmp_path / "archive.zip"
    path.write_text("nope", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported file type"):
        load_document(path)


def test_load_documents_walks_subdirectories_and_dedupes_ids(tmp_path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "overview.md").write_text("# A\n\nalpha", encoding="utf-8")
    (tmp_path / "b" / "overview.md").write_text("# B\n\nbeta", encoding="utf-8")
    documents = load_documents(tmp_path)
    assert len(documents) == 2
    assert len({document.doc_id for document in documents}) == 2


def test_load_documents_rejects_empty_corpus(tmp_path) -> None:
    (tmp_path / "notes.rtf").write_text("unsupported", encoding="utf-8")
    with pytest.raises(ValueError, match="no supported files"):
        load_documents(tmp_path)
