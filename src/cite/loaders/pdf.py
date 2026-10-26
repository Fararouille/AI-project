"""PDF loading with per-page section markers and optional OCR.

Extraction quality is a property of the PDF, not of this code. Text-based PDFs
usually come out fine; scanned PDFs contain images and have no text layer. Rather
than silently indexing an empty document, this loader either OCRs the page when
the optional toolchain is installed or raises an error that says exactly what is
missing.

OCR is opt-in by dependency, never by default install:

    pip install -e '.[ocr]'
    # plus the Tesseract binary for your platform

``CITE_PDF_OCR`` controls the behaviour: ``auto`` (default) OCRs only pages whose
text layer is empty, ``always`` OCRs every page, and ``off`` never OCRs. Each page
becomes a ``## Page N`` section, so a citation can point at a page instead of the
whole file and the chunker still gets useful breadcrumbs.
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path

from .base import LoadedDocument, MissingLoaderDependency, title_from_filename

_BLANK_LINES_RE = re.compile(r"\n{3,}")
_SPACE_RE = re.compile(r"[ \t\u00a0]+")
_MIN_TEXT_CHARS = 20
_OCR_MODES = {"auto", "always", "off"}
_INSTALL_HINT = (
    "install the optional OCR toolchain with `pip install -e '.[ocr]'` and the "
    "Tesseract binary (https://tesseract-ocr.github.io/tessdoc/Installation.html)"
)


def _clean_page(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _SPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


class _OCRRenderer:
    """Lazy bridge to PyMuPDF + Tesseract, loaded only when a page needs it."""

    def __init__(self, path: Path, required: bool) -> None:
        self.path = path
        self.required = required
        self._document = None
        self._pytesseract = None
        self._image = None
        self._failed = False

    def _load(self) -> bool:
        if self._document is not None:
            return True
        if self._failed:
            return False
        try:
            try:
                import pymupdf
            except ImportError:  # PyMuPDF < 1.24 only shipped the fitz name
                import fitz as pymupdf
            import pytesseract
            from PIL import Image
        except ImportError:
            self._failed = True
            if self.required:
                raise MissingLoaderDependency(
                    f"{self.path}: OCR is required but its dependencies are missing; "
                    f"{_INSTALL_HINT}"
                ) from None
            return False
        self._document = pymupdf.open(str(self.path))
        self._pytesseract = pytesseract
        self._image = Image
        return True

    def text(self, number: int) -> str | None:
        """OCR one page, or return ``None`` when the toolchain is unavailable."""
        if not self._load():
            return None
        try:
            page = self._document[number - 1]
            pixmap = page.get_pixmap(dpi=200)
            image = self._image.open(io.BytesIO(pixmap.tobytes("png")))
            return _clean_page(self._pytesseract.image_to_string(image))
        except Exception as exc:
            if self.required:
                raise MissingLoaderDependency(
                    f"{self.path}: OCR failed on page {number} ({exc}); {_INSTALL_HINT}"
                ) from exc
            return None

    def close(self) -> None:
        if self._document is not None:
            self._document.close()
            self._document = None


class PDFLoader:
    name = "pdf"
    suffixes = frozenset({".pdf"})

    def __init__(self, ocr: str | None = None) -> None:
        self.ocr = ocr

    def _mode(self) -> str:
        value = (self.ocr or os.getenv("CITE_PDF_OCR", "auto")).strip().lower()
        return value if value in _OCR_MODES else "auto"

    def load(self, path: Path) -> LoadedDocument:
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - exercised only without extras
            raise MissingLoaderDependency(
                "PDF ingestion needs pypdf; install it with "
                "`pip install -e '.[loaders]'` or `pip install pypdf`"
            ) from exc

        mode = self._mode()
        reader = PdfReader(str(path))
        title = self._title(reader, path)
        renderer = _OCRRenderer(path, required=mode == "always") if mode != "off" else None
        pages: list[str] = []
        try:
            for number, page in enumerate(reader.pages, start=1):
                body = _clean_page(page.extract_text() or "")
                if renderer is not None and (mode == "always" or len(body) < _MIN_TEXT_CHARS):
                    extracted = renderer.text(number)
                    if extracted and len(extracted) > len(body):
                        body = extracted
                if body:
                    pages.append(f"## Page {number}\n\n{body}")
        finally:
            if renderer is not None:
                renderer.close()

        if not pages:
            raise ValueError(
                f"{path}: no extractable text. The PDF is probably scanned; "
                f"{_INSTALL_HINT}, or run OCR before ingesting it."
            )
        return LoadedDocument(title=title, text=f"# {title}\n\n" + "\n\n".join(pages))

    @staticmethod
    def _title(reader, path: Path) -> str:
        try:
            metadata_title = (reader.metadata or {}).get("/Title")
        except Exception:  # pragma: no cover - malformed metadata is not fatal
            metadata_title = None
        if isinstance(metadata_title, str) and metadata_title.strip():
            return metadata_title.strip()
        return title_from_filename(path)
