"""HTML loading.

Real HTML is mostly navigation, footers, cookie banners and script tags. Feeding
raw markup to a chunker wastes the index on chrome and can invent answers from
JavaScript source. This loader drops non-content elements first, then converts the
remaining block structure to Markdown so the existing heading-aware chunker keeps
working unchanged.

``beautifulsoup4`` is an optional dependency (``pip install cite[loaders]``). The
import lives inside :meth:`HTMLLoader.load` so the offline default install stays
small.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import LoadedDocument, MissingLoaderDependency, title_from_filename

_SKIP_TAGS = ("script", "style", "nav", "footer", "aside", "noscript", "svg", "form", "iframe")
_BLOCK_TAGS = (
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "li",
    "pre",
    "blockquote",
    "table",
    "tr",
)
_WHITESPACE_RE = re.compile(r"[ \t\u00a0]+")


def _clean(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value).strip()


def _cells(row) -> tuple[list[str], bool]:
    """Return a row's cell text and whether it is the first row of its table."""
    values = [_clean(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
    return values, row.find_previous_sibling("tr") is None


def _render_table(table) -> str:
    """Render a whole table as one Markdown block.

    Emitting each ``<tr>`` as its own paragraph would separate the header from
    the rows, so a chunk would lose the column names it needs to be understood.
    Padding keeps ragged rows valid Markdown.
    """
    rows: list[list[str]] = []
    for row in table.find_all("tr"):
        if row.find_parent("table") is not table:
            continue
        values, _ = _cells(row)
        if values:
            rows.append(values)
    if not rows:
        return ""
    width = max(len(values) for values in rows)
    rendered: list[str] = []
    for position, values in enumerate(rows):
        padded = values + [""] * (width - len(values))
        rendered.append("| " + " | ".join(padded) + " |")
        if position == 0:
            rendered.append("|" + "---|" * width)
    return "\n".join(rendered)


class HTMLLoader:
    name = "html"
    suffixes = frozenset({".html", ".htm"})

    def load(self, path: Path) -> LoadedDocument:
        try:
            from bs4 import BeautifulSoup
        except ImportError as exc:  # pragma: no cover - exercised only without extras
            raise MissingLoaderDependency(
                "HTML ingestion needs beautifulsoup4; install it with "
                "`pip install -e '.[loaders]'` or `pip install beautifulsoup4`"
            ) from exc

        soup = BeautifulSoup(path.read_text(encoding="utf-8-sig", errors="replace"), "html.parser")
        for tag in soup.find_all(_SKIP_TAGS):
            tag.decompose()

        title = self._title(soup, path)
        lines: list[str] = []
        for element in soup.find_all(list(_BLOCK_TAGS)):
            # Keep only the outermost block. A <p> inside a <blockquote> is already
            # represented by the quote, and rendering both duplicates the text.
            if element.find_parent(list(_BLOCK_TAGS)) is not None:
                continue
            rendered = self._render(element)
            if rendered:
                lines.append(rendered)

        if not lines:
            # Some pages are just loose text nodes and <br> tags.
            loose = _clean(soup.get_text("\n", strip=True))
            if loose:
                lines.append(loose)

        return LoadedDocument(title=title, text="\n\n".join(lines))

    @staticmethod
    def _title(soup, path: Path) -> str:
        heading = soup.find("h1")
        if heading:
            value = _clean(heading.get_text(" ", strip=True))
            if value:
                return value
        if soup.title:
            value = _clean(soup.title.get_text(" ", strip=True))
            if value:
                return value
        return title_from_filename(path)

    @staticmethod
    def _render(element) -> str:
        name = element.name
        if name == "table":
            return _render_table(element)
        if name == "tr":
            values, first = _cells(element)
            if not values:
                return ""
            row = "| " + " | ".join(values) + " |"
            if first:
                return row + "\n|" + "---|" * len(values)
            return row
        if name.startswith("h") and len(name) == 2 and name[1].isdigit():
            value = _clean(element.get_text(" ", strip=True))
            return f"{'#' * int(name[1])} {value}" if value else ""
        if name == "li":
            value = _clean(element.get_text(" ", strip=True))
            return f"- {value}" if value else ""
        if name == "blockquote":
            value = _clean(element.get_text(" ", strip=True))
            return f"> {value}" if value else ""
        if name == "pre":
            value = element.get_text("", strip=False).strip("\n")
            return f"```\n{value}\n```" if value.strip() else ""
        value = _clean(element.get_text(" ", strip=True))
        return value
