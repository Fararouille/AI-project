| Configuration | Recall@k | MRR | nDCG@k | p50 (ms) | p95 (ms) |
|---|---:|---:|---:|---:|---:|
| BM25 only | 0.985 | 0.934 | 0.937 | 0.04 | 0.07 |
| Dense only | 0.941 | 0.806 | 0.834 | 3.36 | 4.19 |
| Hybrid (RRF) | 0.956 | 0.904 | 0.910 | 3.42 | 3.88 |
| Hybrid + rerank | 0.956 | 0.919 | 0.918 | 4.03 | 4.39 |

k = 5, 34 queries (embedder: hashing).
