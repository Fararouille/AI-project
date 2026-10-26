"""Browser uploads: validate one file, store it, and remove it again.

Uploads are kept outside the corpus directory so the checked-in corpus stays
read-only and a container can mount a volume over ``data/uploads`` without
touching the image. The index is still rebuilt from text, not from the uploaded
bytes: the file is parsed with the same loader as ``cite ingest`` and the
extracted text becomes chunks.

Two invariants make this safe to expose through an admin-only endpoint:

* the file name is reduced to a slug plus a whitelisted extension, so it cannot
  escape the upload directory;
* the parsed text must be non-empty before the file replaces an existing upload,
  so a corrupt PDF cannot silently evict a good document.

The API rebuilds the collection after a successful write, reusing every unchanged
chunk. Callers get a :class:`StoredUpload` that can restore the previous bytes if
the rebuild fails.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .loaders import SUPPORTED_SUFFIXES, load_document
from .text import slug

MAX_FILENAME_CHARS = 120


class UploadError(ValueError):
    """The uploaded file cannot be stored as a document."""


def document_id_for(filename: str | Path) -> str:
    """The document id an uploaded file will get, matching ``cite ingest``."""
    return slug(Path(filename).stem) or "document"


def safe_filename(raw: str | None) -> str:
    """Reduce a browser-supplied name to its last component and check its type."""
    if not raw or not raw.strip():
        raise UploadError("a file name is required")
    # Windows clients may send a full path with backslashes; both separators are
    # stripped before the last component is taken.
    name = Path(raw.replace("\\", "/")).name.strip()
    if not name or name in {".", ".."}:
        raise UploadError("a file name is required")
    if len(name) > MAX_FILENAME_CHARS:
        raise UploadError(f"file name is longer than {MAX_FILENAME_CHARS} characters")
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise UploadError(
            f"unsupported file type {suffix or '(none)'!r}; supported: {supported}"
        )
    return name


@dataclass(frozen=True, slots=True)
class StoredUpload:
    """A file written to the upload directory, with enough state to undo it."""

    doc_id: str
    title: str
    path: Path
    # Previous bytes, including any same-id file with another extension. Restoring
    # them leaves the pre-upload state intact when the index rebuild fails.
    backups: tuple[tuple[Path, bytes], ...] = field(default=(), repr=False)

    def restore(self) -> None:
        self.path.unlink(missing_ok=True)
        for path, data in self.backups:
            path.write_bytes(data)


@dataclass(frozen=True, slots=True)
class StagedRemoval:
    """Uploaded files moved aside so a rebuild can exclude them safely."""

    doc_id: str
    staged: tuple[tuple[Path, Path], ...] = field(default=(), repr=False)

    def discard(self) -> None:
        for _, trash in self.staged:
            trash.unlink(missing_ok=True)

    def restore(self) -> None:
        for original, trash in self.staged:
            if trash.exists():
                os.replace(trash, original)


def stored_documents(settings: Settings, collection: str | None = None) -> dict[str, Path]:
    """Uploaded documents for a collection, keyed by the id they are indexed under."""
    directory = settings.uploads_path_for(collection)
    if not directory.exists():
        return {}
    found: dict[str, Path] = {}
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            found[document_id_for(path)] = path
    return found


def store_upload(
    settings: Settings,
    collection: str | None,
    filename: str | None,
    data: bytes,
) -> StoredUpload:
    """Write a validated upload, replacing a same-id file atomically."""
    name = safe_filename(filename)
    if not data:
        raise UploadError("the uploaded file is empty")

    doc_id = document_id_for(name)
    suffix = Path(name).suffix.lower()
    directory = settings.uploads_path_for(collection)
    directory.mkdir(parents=True, exist_ok=True)

    target = directory / f"{doc_id}{suffix}"
    # Stage in a sibling directory, not inside `directory`: a concurrent rebuild
    # walks the collection recursively, and a half-written file must never look
    # like a document. The leading underscore cannot be a collection name, and
    # the same filesystem keeps the final os.replace atomic.
    staging_root = settings.uploads_dir / "_staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{doc_id}-", dir=staging_root))
    # Keep the document's own stem so a loader that falls back to the filename
    # (a file with no H1) still reports a clean title instead of a temp name.
    temporary = staging / f"{doc_id}{suffix}"
    same_id = [
        path
        for path in directory.iterdir()
        if path.is_file()
        and path != target
        and path.suffix.lower() in SUPPORTED_SUFFIXES
        and document_id_for(path) == doc_id
    ]
    backups = tuple((path, path.read_bytes()) for path in (target, *same_id) if path.exists())

    try:
        temporary.write_bytes(data)
        try:
            loaded = load_document(temporary)
        except (ValueError, OSError) as exc:
            raise UploadError(f"{name}: {exc}") from exc
        if not loaded.text.strip():
            raise UploadError(
                f"{name}: no extractable text; empty documents are not indexable"
            )
        os.replace(temporary, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        try:
            staging_root.rmdir()
        except OSError:
            # Another upload is still staging a file; leave the shared directory.
            pass

    # One document id, one file. Uploading report.pdf after report.md replaces the
    # Markdown rather than leaving two files that slug to the same id.
    for path in same_id:
        path.unlink(missing_ok=True)

    return StoredUpload(
        doc_id=doc_id,
        title=loaded.title,
        path=target,
        backups=backups,
    )


def stage_removal(
    settings: Settings,
    collection: str | None,
    doc_id: str,
) -> StagedRemoval:
    """Move an uploaded document aside before rebuilding without it.

    Renaming to an unsupported suffix makes the file invisible to ``load_documents``
    without deleting it, so a failed rebuild can put it back.
    """
    removed: list[tuple[Path, Path]] = []
    for found_id, source in sorted(stored_documents(settings, collection).items()):
        if found_id != doc_id:
            continue
        trash = source.with_name(source.name + ".deleted")
        os.replace(source, trash)
        removed.append((source, trash))
    if not removed:
        raise FileNotFoundError(
            f"{doc_id!r} is not an uploaded document; corpus files are removed on disk"
        )
    return StagedRemoval(doc_id=doc_id, staged=tuple(removed))
