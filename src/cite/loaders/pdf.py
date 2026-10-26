"""PDF loading with per-page section markers.

Extraction quality is a property of the PDF, not of this code. Text-based PDFs
usually come out fine; scanned PDFs contain images and need OCR before ingestion.
Rather than silently indexing an empty document, this loader raises a clear error
telling the operator what happened.

Each page becomes a ``## Page N`` section, so a citation can point at a page
instead of the whole file and the chunker still gets useful breadcrumbs.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import LoadedDocument, MissingLoaderDependency, title_from_filename

_BLANK_LINES_RE = re.compile(r"\n{3,}")
_SPACE_RE = re.compile(r"[ \t\u00a0]+")


def _clean_page(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _SPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


class PDFLoader:
    name = "pdf"
    suffixes = frozenset({".pdf"})

    def load(self, path: Path) -> LoadedDocument:
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - exercised only without extras
            raise MissingLoaderDependency(
                "PDF ingestion needs pypdf; install it with "
                "`pip install -e '.[loaders]'` or `pip install pypdf`"
            ) from exc

        reader = PdfReader(str(path))
        title = self._title(reader, path)
        pages: list[str] = []
        for number, page in enumerate(reader.pages, start=1):
            body = _clean_page(page.extract_text() or "")
            if body:
                pages.append(f"## Page {number}\n\n{body}")

        if not pages:
            raise ValueError(
                f"{path}: no extractable text. The PDF is probably scanned; run OCR "
                "before ingesting it."
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
