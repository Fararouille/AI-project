| Configuration | Recall@k | MRR | nDCG@k | p50 (ms) | p95 (ms) |
|---|---:|---:|---:|---:|---:|
| BM25 only | 0.985 | 0.934 | 0.937 | 0.03 | 0.08 |
| Dense only | 0.941 | 0.806 | 0.834 | 2.98 | 3.74 |
| Hybrid (RRF) | 0.956 | 0.904 | 0.910 | 3.03 | 3.34 |
| Hybrid + rerank | 0.956 | 0.919 | 0.918 | 3.46 | 3.80 |

k = 5, 34 queries (embedder: hashing).

### Answer-level checks

| Configuration | Answered | Grounded | Citation P | Citation R | Lexical support | Unsupported | Expected facts |
|---|---:|---:|---:|---:|---:|---:|---:|
| mock provider | 1.000 | 1.000 | 0.819 | 0.838 | 1.000 | 0.000 | 0.603 |

k = 5, 34 queries, provider = mock, mode = bm25. Expected-fact recall is averaged over 34 questions that declare `expects:`.
Lexical support is a proxy: a sentence counts as supported when at least 60% of its content terms appear in the chunk it cites. It catches unsupported or mis-cited sentences, not every form of hallucination; see the README's limitations.
