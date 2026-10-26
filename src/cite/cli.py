"""Command line interface: ingest, ask, evaluate, serve.

Every command prints something a human can use. ``cite eval`` writes a Markdown
table you can paste into a README; ``cite ask --json`` is scriptable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .answer import answer_question
from .config import Settings
from .embeddings import get_embedder
from .evaluation import load_golden, render_markdown, run_ablations
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


def cmd_ingest(args: argparse.Namespace) -> int:
    embedder = get_embedder(args.embedder)
    index = build_index(args.corpus, args.max_words, args.overlap, embedder)
    destination = save_index(index, args.out)
    documents = {chunk.doc_id for chunk in index.chunks}
    print(
        f"indexed {len(documents)} documents into {len(index)} chunks "
        f"-> {destination} (embedder: {embedder.name})"
    )
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    index = load_index(args.index, get_embedder(args.embedder))
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
    index = load_index(args.index, get_embedder(args.embedder))
    golden = load_golden(args.golden)
    results = run_ablations(index, golden.cases, k=golden.k)
    table = render_markdown(results, index.embedder.name)
    print(table)
    if args.out:
        Path(args.out).write_text(table + "\n", encoding="utf-8")
        print(f"\nwrote {args.out}", file=sys.stderr)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("cite.api:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    index_path = Path(args.index)
    print(f"cite {__version__}")
    print(f"corpus: {settings.corpus_dir}")
    print(
        f"index:  {index_path} "
        f"({'present' if index_path.exists() else 'missing - run cite ingest'})"
    )
    print(f"golden: {settings.golden_path}")
    if index_path.exists():
        index = load_index(index_path, get_embedder(args.embedder))
        documents = {chunk.doc_id for chunk in index.chunks}
        print(
            f"loaded: {len(documents)} documents, {len(index)} chunks, "
            f"embedder={index.embedder.name}"
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
    ingest.add_argument("--out", default=str(settings.index_path))
    ingest.add_argument("--max-words", type=int, default=170)
    ingest.add_argument("--overlap", type=int, default=40)
    ingest.set_defaults(func=cmd_ingest)

    ask = subparsers.add_parser("ask", help="ask a question against the index")
    ask.add_argument("question")
    ask.add_argument("--index", default=str(settings.index_path))
    ask.add_argument("--provider", default=None)
    ask.add_argument("--json", action="store_true")
    _add_retrieval_flags(ask)
    ask.set_defaults(func=cmd_ask)

    evaluate = subparsers.add_parser("eval", help="run retrieval ablations against the golden set")
    evaluate.add_argument("--index", default=str(settings.index_path))
    evaluate.add_argument("--golden", default=str(settings.golden_path))
    evaluate.add_argument("--out", help="write the Markdown table to this path")
    evaluate.set_defaults(func=cmd_eval)

    serve = subparsers.add_parser("serve", help="run the HTTP API and web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    info = subparsers.add_parser("info", help="show configuration and index status")
    info.add_argument("--index", default=str(settings.index_path))
    info.set_defaults(func=cmd_info)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
