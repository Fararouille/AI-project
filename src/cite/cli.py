"""Command line interface: ingest, ask, evaluate, serve.

Every command prints something a human can use. ``cite eval`` writes a Markdown
report you can paste into a README; ``cite ask --json`` is scriptable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .answer import answer_question
from .config import Settings, normalize_collection
from .embeddings import get_embedder
from .evaluation import (
    load_golden,
    render_answer_markdown,
    render_markdown,
    run_ablations,
    run_answer_eval,
)
from .ingest import build_index, load_index, save_index
from .providers import get_provider


def _add_retrieval_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mode",
        choices=["auto", "bm25", "dense", "hybrid"],
        default="auto",
        help="retrieval strategy; auto picks bm25 for lexical embedders, hybrid otherwise",
    )
    parser.add_argument(
        "--rerank",
        action="store_true",
        help="enable the heuristic reranker (measured: no gain on the golden set)",
    )
    parser.add_argument("--k", type=int, default=5, help="number of chunks to retrieve")


def _index_path(args: argparse.Namespace, settings: Settings) -> Path:
    explicit = getattr(args, "index", None)
    if explicit:
        return Path(explicit)
    return settings.index_path_for(getattr(args, "collection", None))


def _open_index(path: Path, embedder_name: str | None):
    # With no explicit --embedder (and no CITE_EMBEDDER), trust the embedder
    # recorded in the index. This makes a hosted-embedder index usable without
    # having to repeat --embedder on every command.
    explicit = embedder_name or os.getenv("CITE_EMBEDDER")
    return load_index(path, get_embedder(explicit) if explicit else None)


def cmd_ingest(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    collection = normalize_collection(args.collection, settings.default_collection)
    destination = Path(args.out) if args.out else settings.index_path_for(collection)
    embedder = get_embedder(args.embedder)

    previous = None
    if destination.exists() and not args.full:
        try:
            previous = load_index(destination, embedder, require_match=False)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"[cite] could not read previous index ({exc}); rebuilding", file=sys.stderr)

    index = build_index(
        args.corpus,
        args.max_words,
        args.overlap,
        embedder,
        previous=previous,
        incremental=not args.full,
        collection=collection,
    )
    destination = save_index(index, destination)
    stats = index.stats
    documents = len(index.manifest.get("documents", {}))
    print(
        f"indexed {documents} documents into {len(index)} chunks "
        f"-> {destination} (embedder: {embedder.name}, collection: {collection})"
    )
    print(
        f"{stats.chunks_reused} chunks reused, {stats.chunks_embedded} embedded "
        f"in {stats.elapsed_ms:.1f} ms"
    )
    if stats.full_rebuild:
        print(f"full rebuild: {stats.reason}")
    else:
        print(
            f"incremental: {stats.documents_added} added, {stats.documents_changed} changed, "
            f"{stats.documents_removed} removed, {stats.documents_unchanged} unchanged"
        )
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    index = _open_index(_index_path(args, settings), args.embedder)
    answer = answer_question(
        index,
        args.question,
        provider=get_provider(args.provider),
        k=args.k,
        mode=args.mode,
        rerank=args.rerank,
    )
    if args.json:
        payload = {
            "question": answer.question,
            "answer": answer.text,
            "collection": normalize_collection(args.collection, settings.default_collection),
            "citations": [
                {
                    "marker": citation.marker,
                    "doc_id": citation.doc_id,
                    "title": citation.doc_title,
                    "heading": citation.heading,
                    "snippet": citation.snippet,
                }
                for citation in answer.citations
            ],
            "latency_ms": answer.latency_ms,
            "mode": answer.retrieval_mode,
            "reranked": answer.reranked,
            "provider": answer.provider,
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(answer.text)
    if answer.citations:
        print("\nSources")
        for citation in answer.citations:
            location = (
                f"{citation.doc_title} > {citation.heading}"
                if citation.heading
                else citation.doc_title
            )
            print(f"  [{citation.marker}] {location}")
    else:
        print("\n(no citations - treat this answer with suspicion)", file=sys.stderr)
    print(
        f"\n{answer.latency_ms:.1f} ms | {answer.retrieval_mode}"
        f"{' + rerank' if answer.reranked else ''} | {answer.provider}",
        file=sys.stderr,
    )
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    index = _open_index(_index_path(args, settings), args.embedder)
    golden = load_golden(args.golden)
    results = run_ablations(index, golden.cases, k=golden.k)
    report = render_markdown(results, index.embedder.name)

    if not args.no_answers:
        answer_result = run_answer_eval(
            index,
            golden.cases,
            provider=get_provider(args.provider),
            k=golden.k,
            mode="auto",
        )
        report += "\n\n### Answer-level checks\n\n" + render_answer_markdown([answer_result])

    print(report)
    if args.out:
        Path(args.out).write_text(report + "\n", encoding="utf-8")
        print(f"\nwrote {args.out}", file=sys.stderr)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("cite.api:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    index_path = _index_path(args, settings)
    print(f"cite {__version__}")
    print(f"corpus: {settings.corpus_dir}")
    print(
        f"index:  {index_path} "
        f"({'present' if index_path.exists() else 'missing - run cite ingest'})"
    )
    print(f"golden: {settings.golden_path}")
    print(f"collections: {', '.join(settings.collection_names())}")
    print(
        f"auth: {'enabled' if settings.auth_enabled else 'disabled'} | "
        f"rate limit: {settings.rate_limit_per_minute}/min per identity"
    )
    if index_path.exists():
        index = _open_index(index_path, args.embedder)
        documents = len(index.manifest.get("documents", {})) or len(
            {chunk.doc_id for chunk in index.chunks}
        )
        print(
            f"loaded: {documents} documents, {len(index)} chunks, "
            f"embedder={index.embedder.name}, vectors={len(index.dense.vectors)}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(prog="cite", description=__doc__)
    parser.add_argument("--version", action="version", version=f"cite {__version__}")
    parser.add_argument("--embedder", help="hashing (default) or openai")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="chunk the corpus and write an index")
    ingest.add_argument("--corpus", default=str(settings.corpus_dir))
    ingest.add_argument("--out", help="index path; defaults to the collection's path")
    ingest.add_argument("--collection", default=settings.default_collection)
    ingest.add_argument("--max-words", type=int, default=170)
    ingest.add_argument("--overlap", type=int, default=40)
    ingest.add_argument(
        "--full",
        action="store_true",
        help="ignore the previous manifest and re-embed every chunk",
    )
    ingest.set_defaults(func=cmd_ingest)

    ask = subparsers.add_parser("ask", help="ask a question against the index")
    ask.add_argument("question")
    ask.add_argument("--index", help="index path; defaults to the collection's path")
    ask.add_argument("--collection", default=settings.default_collection)
    ask.add_argument("--provider", default=None)
    ask.add_argument("--json", action="store_true")
    _add_retrieval_flags(ask)
    ask.set_defaults(func=cmd_ask)

    evaluate = subparsers.add_parser("eval", help="run retrieval ablations against the golden set")
    evaluate.add_argument("--index", help="index path; defaults to the collection's path")
    evaluate.add_argument("--collection", default=settings.default_collection)
    evaluate.add_argument("--golden", default=str(settings.golden_path))
    evaluate.add_argument("--provider", default=None)
    evaluate.add_argument(
        "--no-answers",
        action="store_true",
        help="skip answer-level checks (retrieval metrics only)",
    )
    evaluate.add_argument("--out", help="write the Markdown report to this path")
    evaluate.set_defaults(func=cmd_eval)

    serve = subparsers.add_parser("serve", help="run the HTTP API and web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    info = subparsers.add_parser("info", help="show configuration and index status")
    info.add_argument("--index", help="index path; defaults to the collection's path")
    info.add_argument("--collection", default=settings.default_collection)
    info.set_defaults(func=cmd_info)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
