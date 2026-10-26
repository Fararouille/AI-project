from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from cite.ingest import load_documents
from cite.loaders import SUPPORTED_SUFFIXES, get_loader, load_document
from cite.loaders.base import MissingLoaderDependency
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


def test_markdown_ignores_a_utf8_bom_before_the_title(tmp_path) -> None:
    path = tmp_path / "bom.md"
    path.write_bytes("# Real Title\n\nbody".encode("utf-8-sig"))
    loaded = load_document(path)
    assert loaded.title == "Real Title"
    assert not loaded.text.startswith("\ufeff")


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


def _write_scanned_pdf(path: Path) -> None:
    """Render text to an image, then embed the image in a PDF with no text layer."""
    pymupdf = pytest.importorskip("pymupdf")
    source = pymupdf.open()
    page = source.new_page(width=520, height=160)
    page.insert_text((24, 90), "Scanned widget limit is 250 per day.", fontsize=20)
    pixmap = page.get_pixmap(dpi=200)
    scanned = pymupdf.open()
    target = scanned.new_page(width=520, height=160)
    target.insert_image(target.rect, pixmap=pixmap)
    scanned.save(str(path))
    scanned.close()
    source.close()


def test_scanned_pdf_with_ocr_off_fails_loudly(tmp_path, monkeypatch) -> None:
    path = tmp_path / "scanned.pdf"
    _write_scanned_pdf(path)
    monkeypatch.setenv("CITE_PDF_OCR", "off")
    with pytest.raises(ValueError, match="no extractable text"):
        load_document(path)
    with pytest.raises(ValueError, match=r"\[ocr\]"):
        load_document(path)


def test_scanned_pdf_is_ocred_when_the_toolchain_is_installed(
    tmp_path, monkeypatch
) -> None:
    pytest.importorskip("pytesseract")
    pytest.importorskip("PIL")
    if shutil.which("tesseract") is None:
        pytest.skip("Tesseract binary is not installed")
    path = tmp_path / "scanned.pdf"
    _write_scanned_pdf(path)
    monkeypatch.setenv("CITE_PDF_OCR", "auto")
    text = load_document(path).text
    assert "250" in text


def _write_blank_pdf(path: Path) -> None:
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(str(path))


def test_ocr_always_mode_explains_missing_dependencies(tmp_path, monkeypatch) -> None:
    path = tmp_path / "blank.pdf"
    _write_blank_pdf(path)
    for module in ("pymupdf", "fitz", "pytesseract"):
        monkeypatch.setitem(sys.modules, module, None)
    with pytest.raises(MissingLoaderDependency, match="OCR is required"):
        PDFLoader(ocr="always").load(path)


def test_ocr_auto_mode_still_fails_clearly_without_dependencies(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "blank.pdf"
    _write_blank_pdf(path)
    for module in ("pymupdf", "fitz", "pytesseract"):
        monkeypatch.setitem(sys.modules, module, None)
    with pytest.raises(ValueError, match=r"\[ocr\]"):
        PDFLoader(ocr="auto").load(path)


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
