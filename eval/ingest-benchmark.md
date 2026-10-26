# Ingest benchmark

Generated 2026-10-26 on Python 3.12.4 (Windows, single process).

| Corpus | Store | Workload | Documents | Chunks | Total median (ms) | p95 (ms) | Save median (ms) | Chunks embedded | Chunks reused |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| Demo (14 docs) | JSON | Full rebuild | 14 | 104 | 154.8 | 158.7 | 47.8 | 104 | 0 |
| Demo (14 docs) | JSON | One document changed | 14 | 104 | 84.4 | 96.5 | 46.7 | 7 | 97 |
| Demo (14 docs) | JSON | No changes | 14 | 104 | 76.0 | 79.8 | 47.2 | 0 | 104 |
| Demo (14 docs) | SQLite | Full rebuild | 14 | 104 | 126.4 | 170.7 | 11.6 | 104 | 0 |
| Demo (14 docs) | SQLite | One document changed | 14 | 104 | 42.2 | 47.0 | 7.0 | 7 | 97 |
| Demo (14 docs) | SQLite | No changes | 14 | 104 | 24.1 | 31.1 | 4.1 | 0 | 104 |
| Scaled (20x) | JSON | Full rebuild | 280 | 2080 | 3207.5 | 3515.0 | 937.6 | 2080 | 0 |
| Scaled (20x) | JSON | One document changed | 280 | 2080 | 1436.3 | 1540.1 | 971.0 | 7 | 2073 |
| Scaled (20x) | JSON | No changes | 280 | 2080 | 1528.4 | 1529.8 | 1016.2 | 0 | 2080 |
| Scaled (20x) | SQLite | Full rebuild | 280 | 2080 | 2482.9 | 2532.0 | 125.0 | 2080 | 0 |
| Scaled (20x) | SQLite | One document changed | 280 | 2080 | 378.4 | 447.9 | 26.7 | 7 | 2073 |
| Scaled (20x) | SQLite | No changes | 280 | 2080 | 364.3 | 382.0 | 20.5 | 0 | 2080 |

Each row is the median of 8 measured runs after one warmup (scaled rows use fewer iterations). The scaled corpus is the handbook copied 20x into a temporary directory. Total includes build plus save; save is also reported alone.

- Demo corpus, SQLite: a one-document edit is **3.0x** faster than a full rebuild and embeds 97 fewer chunks.
- 20x corpus, SQLite: a one-document edit is **6.6x** faster and embeds 2073 fewer chunks.
- On the same edit, the save step takes **971.0 ms for JSON vs 26.7 ms for SQLite**. JSON rewrites every chunk and vector; SQLite writes one document's rows.

**What this does not claim.** `chunks_embedded`, not wall-clock time, is the number that costs money with a hosted embedder; it drops from the whole corpus to the edited document in both stores. SQLite removes the full serialisation, but BM25 is still rebuilt in memory on every load, the whole corpus is still scanned on every ingest, and one file means one writer. A multi-writer deployment needs a vector server and a queue, not another local file format.

Reproduce with `python scripts/benchmark_ingest.py --out eval/ingest-benchmark.md`.
