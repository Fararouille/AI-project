# Ingest benchmark

Generated 2026-10-26 on Python 3.12.4 (Windows, single process).

| Corpus | Workload | Documents | Chunks | Median (ms) | p95 (ms) | Chunks embedded | Chunks reused |
|---|---|---:|---:|---:|---:|---:|---:|
| Demo (14 docs) | Full rebuild | 14 | 104 | 157.7 | 178.0 | 104 | 0 |
| Demo (14 docs) | One document changed | 14 | 104 | 91.5 | 131.5 | 7 | 97 |
| Demo (14 docs) | No changes | 14 | 104 | 76.6 | 91.8 | 0 | 104 |
| Scaled (20x) | Full rebuild | 280 | 2080 | 3343.6 | 3433.6 | 2080 | 0 |
| Scaled (20x) | One document changed | 280 | 2080 | 1453.6 | 1551.8 | 7 | 2073 |
| Scaled (20x) | No changes | 280 | 2080 | 1437.9 | 1523.8 | 0 | 2080 |

Each row is the median of 12 measured runs after one warmup. The scaled corpus is the handbook copied 20x into a temporary directory.

- Demo corpus: a one-document edit is **1.7x** faster than a full rebuild and embeds 97 fewer chunks.
- 20x corpus: a one-document edit is **2.3x** faster and embeds 2073 fewer chunks.

**What this does not claim.** The index is still rewritten in full on every save, so wall-clock time does not fall to zero; only chunking and embedding are skipped. The offline `hashing` embedder is cheap, which makes the speedup look modest. With a hosted embedder the cost that matters is `chunks_embedded`, and that drops from the whole corpus to the edited document. Vector serialisation also grows the index file; a production system would move vectors to a store that supports partial updates.

Reproduce with `python scripts/benchmark_ingest.py --out eval/ingest-benchmark.md`.
