"""Measure full rebuild vs incremental re-indexing, JSON vs SQLite storage.

The important number is ``chunks_embedded``. Wall-clock time depends on the
machine and on the offline hashing embedder; embedder calls are what cost money
when a hosted model is used. Both are reported so the claim can be checked.

The second question is what a save costs. JSON serialises the entire index on
every write; the SQLite store updates rows for added, changed, and removed
documents only. The script reports total time and save-only time separately,
because a fast build with a slow save is still a slow deploy.

Run from the repository root:

    python scripts/benchmark_ingest.py --out eval/ingest-benchmark.md

The script copies the corpus into a temporary directory, edits exactly one
document between incremental runs, and never touches ``data/corpus``.
"""

from __future__ import annotations

import argparse
import math
import platform
import shutil
import statistics
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from cite.embeddings import HashingEmbedder
from cite.ingest import build_index, load_index, save_index

ROOT = Path(__file__).resolve().parents[1]
SOURCE_CORPUS = ROOT / "data" / "corpus"
SUFFIXES = {"json": ".json", "sqlite": ".sqlite3"}


@dataclass(frozen=True)
class Row:
    corpus: str
    mode: str
    store: str
    documents: int
    chunks: int
    median_ms: float
    p95_ms: float
    save_median_ms: float
    chunks_embedded: int
    chunks_reused: int


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    position = min(len(ordered) - 1, max(0, math.ceil(0.95 * len(ordered)) - 1))
    return ordered[position]


def _copy_corpus(destination: Path) -> Path:
    shutil.copytree(SOURCE_CORPUS, destination)
    return destination


def _scaled_corpus(destination: Path, factor: int) -> Path:
    """Duplicate the handbook to approximate a larger, less toy-like corpus."""
    destination.mkdir(parents=True, exist_ok=True)
    for source in sorted(SOURCE_CORPUS.glob("*.md")):
        text = source.read_text(encoding="utf-8")
        for copy in range(factor):
            target = destination / f"{copy:02d}-{source.name}"
            target.write_text(text, encoding="utf-8")
    return destination


def _run(
    label: str,
    corpus: Path,
    index_path: Path,
    embedder: HashingEmbedder,
    iterations: int,
    mode: str,
    store: str,
    edit_doc: Path | None = None,
) -> Row:
    durations: list[float] = []
    save_durations: list[float] = []
    embedded: list[int] = []
    reused: list[int] = []
    documents = chunks = 0
    original = edit_doc.read_text(encoding="utf-8") if edit_doc else None

    # One warmup build populates the index for incremental modes and warms the
    # filesystem cache; it is not included in the numbers.
    if mode != "full":
        warmup = build_index(corpus, embedder=embedder, incremental=False, collection="bench")
        save_index(warmup, index_path)
    else:
        build_index(corpus, embedder=embedder, incremental=False, collection="bench")

    for run in range(iterations):
        started = time.perf_counter()
        if mode == "full":
            index = build_index(
                corpus, embedder=embedder, incremental=False, collection="bench"
            )
        else:
            previous = load_index(index_path, embedder, require_match=False)
            if mode == "edit" and edit_doc is not None and original is not None:
                edit_doc.write_text(
                    original + f"\n\nBenchmark edit {run}: the retry budget is {run}.\n",
                    encoding="utf-8",
                )
            index = build_index(
                corpus,
                embedder=embedder,
                previous=previous,
                incremental=True,
                collection="bench",
            )
        build_ms = (time.perf_counter() - started) * 1000.0
        save_started = time.perf_counter()
        save_index(index, index_path)
        save_durations.append((time.perf_counter() - save_started) * 1000.0)
        durations.append(build_ms + save_durations[-1])
        assert index.stats is not None
        embedded.append(index.stats.chunks_embedded)
        reused.append(index.stats.chunks_reused)
        documents, chunks = index.stats.documents, index.stats.chunks

    return Row(
        corpus=label,
        mode=mode,
        store=store,
        documents=documents,
        chunks=chunks,
        median_ms=statistics.median(durations),
        p95_ms=_p95(durations),
        save_median_ms=statistics.median(save_durations),
        chunks_embedded=round(statistics.median(embedded)),
        chunks_reused=round(statistics.median(reused)),
    )


def _render(rows: list[Row], iterations: int, scale: int) -> str:
    labels = {
        "full": "Full rebuild",
        "edit": "One document changed",
        "noop": "No changes",
    }
    stores = {"json": "JSON", "sqlite": "SQLite"}
    lines = [
        "# Ingest benchmark",
        "",
        f"Generated {date.today().isoformat()} on Python {platform.python_version()} "
        f"({platform.system()}, single process).",
        "",
        "| Corpus | Store | Workload | Documents | Chunks | Total median (ms) | "
        "p95 (ms) | Save median (ms) | Chunks embedded | Chunks reused |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row.corpus} | {stores[row.store]} | {labels[row.mode]} | "
            f"{row.documents} | {row.chunks} | {row.median_ms:.1f} | "
            f"{row.p95_ms:.1f} | {row.save_median_ms:.1f} | "
            f"{row.chunks_embedded} | {row.chunks_reused} |"
        )

    def find(corpus_prefix: str, mode: str, store: str) -> Row | None:
        for row in rows:
            if row.corpus.startswith(corpus_prefix) and row.mode == mode and row.store == store:
                return row
        return None

    lines += [
        "",
        f"Each row is the median of {iterations} measured runs after one warmup "
        f"(scaled rows use fewer iterations). The scaled corpus is the handbook "
        f"copied {scale}x into a temporary directory. Total includes build plus save; "
        "save is also reported alone.",
        "",
    ]
    demo_full = find("Demo", "full", "sqlite")
    demo_edit = find("Demo", "edit", "sqlite")
    if demo_full and demo_edit:
        lines.append(
            f"- Demo corpus, SQLite: a one-document edit is "
            f"**{demo_full.median_ms / demo_edit.median_ms:.1f}x** faster than a "
            f"full rebuild and embeds "
            f"{demo_full.chunks_embedded - demo_edit.chunks_embedded} fewer chunks."
        )
    scaled_full = find("Scaled", "full", "sqlite")
    scaled_edit = find("Scaled", "edit", "sqlite")
    if scaled_full and scaled_edit:
        lines.append(
            f"- {scale}x corpus, SQLite: a one-document edit is "
            f"**{scaled_full.median_ms / scaled_edit.median_ms:.1f}x** faster and "
            f"embeds {scaled_full.chunks_embedded - scaled_edit.chunks_embedded} "
            "fewer chunks."
        )
    json_save = find("Scaled", "edit", "json")
    sqlite_save = find("Scaled", "edit", "sqlite")
    if json_save and sqlite_save:
        lines.append(
            f"- On the same edit, the save step takes "
            f"**{json_save.save_median_ms:.1f} ms for JSON vs "
            f"{sqlite_save.save_median_ms:.1f} ms for SQLite**. JSON rewrites every "
            "chunk and vector; SQLite writes one document's rows."
        )
    lines += [
        "",
        "**What this does not claim.** `chunks_embedded`, not wall-clock time, is "
        "the number that costs money with a hosted embedder; it drops from the whole "
        "corpus to the edited document in both stores. SQLite removes the full "
        "serialisation, but BM25 is still rebuilt in memory on every load, the whole "
        "corpus is still scanned on every ingest, and one file means one writer. "
        "A multi-writer deployment needs a vector server and a queue, not another "
        "local file format.",
        "",
        "Reproduce with `python scripts/benchmark_ingest.py --out eval/ingest-benchmark.md`.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "eval" / "ingest-benchmark.md"))
    parser.add_argument("--iterations", type=int, default=12)
    parser.add_argument("--scale", type=int, default=20)
    parser.add_argument("--scale-iterations", type=int, default=4)
    args = parser.parse_args()

    embedder = HashingEmbedder()
    rows: list[Row] = []
    with tempfile.TemporaryDirectory(prefix="cite-bench-") as temporary:
        base = Path(temporary)
        demo_corpus = _copy_corpus(base / "corpus")
        edit_doc = demo_corpus / "rate-limits.md"

        for store, suffix in SUFFIXES.items():
            demo_index = base / f"demo-index{suffix}"
            rows += [
                _run(
                    "Demo (14 docs)",
                    demo_corpus,
                    demo_index,
                    embedder,
                    args.iterations,
                    "full",
                    store,
                ),
                _run(
                    "Demo (14 docs)",
                    demo_corpus,
                    demo_index,
                    embedder,
                    args.iterations,
                    "edit",
                    store,
                    edit_doc,
                ),
                _run(
                    "Demo (14 docs)",
                    demo_corpus,
                    demo_index,
                    embedder,
                    args.iterations,
                    "noop",
                    store,
                ),
            ]

        scaled_corpus = _scaled_corpus(base / f"scaled-{args.scale}", args.scale)
        scaled_edit = sorted(scaled_corpus.glob("*.md"))[0]
        label = f"Scaled ({args.scale}x)"
        for store, suffix in SUFFIXES.items():
            scaled_index = base / f"scaled-index{suffix}"
            rows += [
                _run(
                    label,
                    scaled_corpus,
                    scaled_index,
                    embedder,
                    args.scale_iterations,
                    "full",
                    store,
                ),
                _run(
                    label,
                    scaled_corpus,
                    scaled_index,
                    embedder,
                    args.scale_iterations,
                    "edit",
                    store,
                    scaled_edit,
                ),
                _run(
                    label,
                    scaled_corpus,
                    scaled_index,
                    embedder,
                    args.scale_iterations,
                    "noop",
                    store,
                ),
            ]

    report = _render(rows, args.iterations, args.scale)
    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(report, encoding="utf-8")
    print(report)
    print(f"wrote {destination}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
