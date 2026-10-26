# cite

**Grounded answers over your own documents, with a retrieval and answer-quality benchmark that runs in one command.**

Most RAG projects demo an answer and stop. `cite` treats retrieval as a measurable
system: it ships a hand-labelled golden set, an ablation harness, answer-level checks,
and the numbers those produce — including the results that did not go the way the
project expected.

```bash
pip install -e ".[dev]"
cite ingest --corpus data/corpus
cite eval
```

No API keys. No network. No GPU. The default embedder is a deterministic feature-hashing
model, so the benchmark is reproducible on any laptop. Swapping in a hosted embedding
model and an LLM is one environment variable.

![cite UI: an answer with inline citations, clickable sources, and the live benchmark panel](docs/screenshot.png)

---

## Headline result

34 hand-labelled queries over a 14-document technical handbook, k = 5:

| Configuration | Recall@5 | MRR | nDCG@5 | p50 (ms) | p95 (ms) |
|---|---:|---:|---:|---:|---:|
| BM25 only | **0.985** | **0.934** | **0.937** | **0.03** | **0.08** |
| Dense only | 0.941 | 0.806 | 0.834 | 2.98 | 3.74 |
| Hybrid (RRF) | 0.956 | 0.904 | 0.910 | 3.03 | 3.34 |
| Hybrid + rerank | 0.956 | 0.919 | 0.918 | 3.46 | 3.80 |

*`hashing` embedder, single core, measured with `cite eval`. Reproduce with
`cite eval --out eval/results.md`.*

### The finding that matters

**The hybrid retriever did not beat BM25.** That is not the story the project set out to
tell, and it is the most useful thing in this repository.

The cause is visible in the ablation: dense retrieval alone loses 5 points of recall and
13 points of MRR against a 30-year-old lexical algorithm. The offline embedder is
*lexical* — it hashes word, bigram, and character n-gram features. Fusing a weak lexical
model with a strong lexical model does not add a second signal; it adds noise, and RRF
duly dilutes a correct rank-1 result.

Two changes followed from measuring instead of assuming:

1. `mode=auto` now selects BM25 for lexical embedders and hybrid only for a semantic
   embedder (`CITE_EMBEDDER=openai`). The product does not ship the configuration its own
   benchmark rejects.
2. The heuristic reranker is off by default. It recovers a little MRR (+0.015) but does
   not beat plain BM25, costs ~12% latency, and adds a component that has to be
   maintained. It stays in the repo as an ablation and as the interface a real
   cross-encoder would implement.

Deleting a component that does not pay for itself is a result. Shipping it anyway is
how systems rot.

### Answer-level result

Retrieval metrics stop at "the right page was found". The harness also runs the whole
answer pipeline over the same questions:

| Configuration | Answered | Grounded | Citation P | Citation R | Lexical support | Unsupported | Expected facts |
|---|---:|---:|---:|---:|---:|---:|---:|
| mock provider | 1.000 | 1.000 | 0.819 | 0.838 | 1.000 | 0.000 | **0.603** |

*34 queries, provider = mock, mode = bm25. Expected-fact recall is averaged over the 34
questions that declare `expects:`.*

Read the columns honestly:

- **Grounded = 1.000 and unsupported = 0.000 are structural, not impressive.** The
  offline provider quotes source sentences verbatim and attaches a marker, so its claims
  are supported by construction. This column proves the citation plumbing works; it does
  not prove a hosted model will behave.
- **Expected-fact recall = 0.603 is the useful number.** The retriever finds the right
  page, but the extractive answerer often selects a nearby sentence instead of the one
  containing the fact — especially facts that live in table rows or JSON examples.
  That gap is exactly what a real provider is for, and the harness can measure whether
  it closes.
- Citation precision/recall are below 1.0 because the labels are document-level and some
  questions accept more than one page; citing a second, plausible page is not a
  hallucination, but the metric counts it as imprecise.

---

## What it actually does

```
                 ┌──────────────┐
 Markdown  ─────▶│  loaders     │  Markdown / text, HTML (BeautifulSoup), PDF (pypdf)
 HTML / PDF      │              │  navigation, scripts and boilerplate removed
                 └──────┬───────┘
                        │
                 ┌──────▼───────┐
                 │  chunker     │  heading-aware, block-aware
                 │              │  (bullets, tables, code stay intact)
                 └──────┬───────┘
                        │ chunks + heading breadcrumbs
                 ┌──────▼───────┐
                 │  manifest    │  SHA-256 per document → chunk ids
                 │              │  unchanged docs reuse chunks + vectors
                 └──────┬───────┘
                 ┌──────▼───────┐
                 │    index     │
                 │  ┌────────┐  │  BM25: inverted index, postings-only scoring
                 │  │  bm25  │  │
                 │  ├────────┤  │  dense: cosine over normalised vectors
                 │  │ dense  │  │
                 │  └────┬───┘  │
                 └───────┼──────┘
                         │  RRF fusion (optional) → heuristic rerank (optional)
                 ┌───────▼──────┐
 question ──────▶│  retriever   │──▶ top-k chunks
                 └───────┬──────┘
                 ┌───────▼──────┐
                 │  provider    │  mock (extractive, default) | OpenAI | Anthropic
                 └───────┬──────┘
                 ┌───────▼──────┐
                 │  citations   │  [n] markers → source, heading, snippet
                 └───────┬──────┘
                 ┌───────▼──────┐
                 │  usage log   │  auth, token bucket, SQLite query log (opt-in)
                 └──────────────┘
```

Every answer carries citations resolved back to the exact chunk, so a wrong answer is
auditable rather than mysterious.

### Ingestion: three formats, incremental rebuilds

Formats are pluggable loaders (`src/cite/loaders/`). Markdown and text have no extra
dependencies; HTML and PDF need the `loaders` extra:

```bash
pip install -e ".[loaders]"     # pypdf + beautifulsoup4
cite ingest --corpus ~/handbook
```

- **Markdown/text** keep their first `#` heading as the title.
- **HTML** drops `script`, `style`, `nav`, `footer`, `aside`, forms and iframes, then
  converts headings, lists, tables and code blocks back to Markdown so the same chunker
  applies. No readability model, no headless browser.
- **PDF** extracts text page by page and inserts `## Page N` headings, so citations can
  point at a page. Scanned PDFs raise a clear "run OCR first" error instead of silently
  indexing nothing.

The first ingest embeds every chunk. The second one does not. Each index carries a
manifest of `document id → SHA-256 → chunk ids`, so an unchanged document is copied
verbatim — chunks and vectors — and only new or edited documents are re-embedded.
Deleted documents are removed. Changing the embedder, the chunking parameters, or the
collection forces a full rebuild rather than silently mixing vector spaces. Writes go to
a temporary file and are swapped with `os.replace`, so a crash cannot leave a half-valid
index.

Measured with `python scripts/benchmark_ingest.py` (median of 12 runs, offline `hashing`
embedder, single process):

| Corpus | Workload | Documents | Chunks | Median (ms) | p95 (ms) | Chunks embedded | Chunks reused |
|---|---|---:|---:|---:|---:|---:|---:|
| Demo (14 docs) | Full rebuild | 14 | 104 | 157.7 | 178.0 | 104 | 0 |
| Demo (14 docs) | One document changed | 14 | 104 | 91.5 | 131.5 | **7** | 97 |
| Demo (14 docs) | No changes | 14 | 104 | 76.6 | 91.8 | **0** | 104 |
| Scaled (20x) | Full rebuild | 280 | 2,080 | 3,343.6 | 3,433.6 | 2,080 | 0 |
| Scaled (20x) | One document changed | 280 | 2,080 | 1,453.6 | 1,551.8 | **7** | 2,073 |
| Scaled (20x) | No changes | 280 | 2,080 | 1,437.9 | 1,523.8 | **0** | 2,080 |

At 20x scale a one-document edit is **2.3x faster in wall-clock time** and makes
**2,073 fewer embedder calls**. The second number is the one that matters with a hosted
model: the offline hashing embedder is cheap, which makes the speedup look modest.

**What this does not claim.** The index file is still rewritten in full on every save,
so wall-clock time does not fall to zero and load time grows with the corpus. Vectors are
stored inline, which makes the JSON index large (~900 KB for 104 chunks at 512
dimensions). A production system would move vectors to a store that supports partial
updates and keep the manifest there. The benchmark is a reproduction of the current
design's behaviour, not a claim that it scales forever.

### The chunker

Chunking is where most RAG quality is won or lost, so it is not a `text.split()`:

- **Headings first.** A chunk never spans two topics. The heading breadcrumb
  (`API > Rate limits`) is prepended to the *indexed* text so short sections are
  findable, but kept out of the *displayed* body so citations read cleanly.
- **Blocks, not characters.** Wrapped prose is joined into paragraphs; each list item,
  table row, and fenced code block stays intact and separately citable.
- **Stable ids.** Chunk ids are content hashes. Re-ingesting the same corpus produces the
  same ids, which is what makes manifest reuse and diffs meaningful.

### The evaluator

- Retrieval metrics: recall@k, MRR, nDCG@k — computed at **document** level, because a
  user cares whether the right page was found, not which slice of it.
- Answer metrics: answered rate, grounded rate, citation precision/recall, lexical
  support, unsupported-claim rate, and expected-fact recall. The golden set can declare
  `expects: ["500 events"]` per question.
- Relevance judgements are hand-written, not generated by the retriever. A golden set
  produced by the system under test measures nothing.
- Ablations are declarative: add a `(name, mode, rerank)` tuple and it appears in the
  table.

Lexical support is a **proxy**: a sentence counts as supported when at least 60% of its
content terms appear in the chunk it cites. It catches uncited and obviously mis-cited
sentences; it cannot detect a fluent paraphrase that contradicts its source. Human review
or an LLM judge is still required for that. The README states this because the metric is
easy to over-read.

### Hardening: auth, rate limits, usage, collections

The default install is still zero-config. Set one environment variable to turn the
production controls on:

| variable | default | effect |
|---|---|---|
| `CITE_API_KEYS` | empty (auth off) | comma-separated keys; enables `X-API-Key` / `Authorization: Bearer` |
| `CITE_RATE_LIMIT_PER_MINUTE` | `60` | per-identity token bucket, `0` disables |
| `CITE_USAGE_DB` | `data/usage.db` | SQLite query log used by `/api/usage` |
| `CITE_LOG_QUESTIONS` | `false` | store raw question text; otherwise only a hash and length |
| `CITE_CORPUS_DIR` | `data/corpus` | source documents |
| `CITE_INDEX_PATH` | `data/index.json` | index for the default collection |
| `CITE_COLLECTIONS_DIR` | `data/collections` | indexes for named collections |
| `CITE_COLLECTION` | `default` | default collection name |
| `CITE_GOLDEN_PATH` | `eval/golden.yaml` | evaluation questions |

```bash
CITE_API_KEYS=dev-secret CITE_RATE_LIMIT_PER_MINUTE=30 cite serve

curl -s localhost:8000/api/ask \
  -H 'X-API-Key: dev-secret' -H 'content-type: application/json' \
  -d '{"question":"How many events fit in one request?"}'

curl -s localhost:8000/api/usage -H 'X-API-Key: dev-secret'
```

Rate limiting is a token bucket per identity, in memory. It is correct for one process
and explicitly **not** a distributed limiter; multiple workers each get their own budget.
`/api/usage` returns request counts, p95 latency, tokens, cost, and a per-collection and
per-mode breakdown. Questions are hashed unless `CITE_LOG_QUESTIONS=1`.

**Collections** are separate index files, not tags on one shared id space:

```bash
cite ingest --corpus ~/handbook --collection handbook
cite ask "How do I rotate a key?" --collection handbook
```

- Default collection: `data/index.json`.
- Named collections: `data/collections/<name>.json`.
- `cite serve` loads all of them at startup; pass `{"collection":"handbook"}` to
  `/api/ask`, or `?collection=handbook` to `/api/sources` and `/api/eval`.
- An unknown collection returns `404`; an invalid name returns `400`.
- `POST /api/reindex` uses the same manifest as the CLI and only re-embeds what changed.

See [`docs/operations.md`](docs/operations.md) for the deployment checklist and the
remaining multi-tenant gaps.

---

## Quickstart

```bash
python -m venv .venv
# Windows: .\.venv\Scripts\activate
source .venv/bin/activate
pip install -e ".[dev]"

cite ingest --corpus data/corpus          # 14 docs → 104 chunks
cite ask "How many tiles can a dashboard have?"
cite eval                                  # retrieval ablations + answer checks
cite serve                                 # API + UI on http://127.0.0.1:8000
```

Example:

```
$ cite ask "Why do my events return 202 but never show up?"

A 202 means the batch was queued, not that it is queryable yet. [1]
Ingestion is asynchronous: an accepted event is queryable within two minutes,
guaranteed within ten. [1]

Sources
  [1] Quickstart > 3. Send a first event

0.4 ms | bm25 | mock
```

### Using a real model

```bash
export OPENAI_API_KEY=sk-...
cite --embedder openai ingest --corpus data/corpus
cite --embedder openai ask "..." --provider openai
```

The provider interface is one method (`generate(question, contexts)`), so adding a new
backend is a class, not a refactor. `mode=auto` switches to hybrid retrieval as soon as
the embedder is semantic. The answer harness does not care which provider is configured;
it runs the same checks and reports the same table.

### Using your own documents

The demo answers from `data/corpus/` — 14 synthetic Markdown pages for a fictional
product, written for this project. Point the ingester at any folder to replace them:

```bash
cite ingest --corpus "$HOME/handbook" --out data/index.json
cite serve
```

- Supported files: `.md`, `.markdown`, `.txt`, `.html`, `.htm`, `.pdf`; subdirectories
  are walked recursively.
- Document id is the filename slug; the title is the first `#` heading (Markdown), the
  `<h1>`/`<title>` (HTML), or PDF metadata, falling back to the filename.
- Headings become the citation breadcrumb, and bullets, tables, and fenced code stay
  intact as separately citable chunks.
- Re-run `cite ingest` after editing documents. It reuses unchanged chunks and vectors.
- `cite serve` loads the index at startup, so **restart it after re-ingesting** unless
  you call `POST /api/reindex`.

`data/index.json` contains the text of every chunk and its vectors — the answer is built
from that text. Keep a private corpus and its index out of version control;
`data/corpus/` is committed here only because it is synthetic.

With a semantic embedder, re-ingest using the same embedder (`--embedder openai`) so the
indexed chunks and the incoming query are embedded in the same space. Changing the
embedder deliberately invalidates the manifest and triggers a full rebuild.

---

## Design decisions, and their costs

| Decision | Why | What it costs |
|---|---|---|
| BM25 by default under a lexical embedder | The benchmark says so | Dense retrieval is unexercised in the default path |
| Hand-written golden set | Prevents circular evaluation | Only 34 queries; growth is manual |
| Separate index file per collection | True id isolation, trivial to reason about | Copies the corpus per collection; no cross-collection search |
| Inline vectors in JSON | One file, no vector database, easy to inspect | Large index and a full rewrite on every save |
| Token bucket in process | No Redis for a single-node demo | Per-worker limits, not shared across replicas |
| SQLite usage log | Queryable without infrastructure | Not a metrics system; single writer |
| Extractive mock provider | Runs offline, never hallucinates | Reads like a quote; expected-fact recall is only 0.603 |
| Heuristic reranker, off by default | Honest about a negative result | No cross-encoder quality without extra work |

### Known limitations

- **Vocabulary mismatch is real.** `"How do I stop my integration from getting
  throttled?"` does not contain the words *rate limit*. BM25 surfaces the right page at
  rank 2 because other query terms happen to match. A semantic embedder is the fix; the
  harness exists to prove it when you plug one in.
- **Answer metrics are proxies.** Groundedness checks citation markers; lexical support
  checks term overlap; expected-fact recall checks substrings. None proves entailment.
  The mock provider's 1.000 support rate is a property of extractive quoting, not
  evidence that grounded generation is solved.
- **The mock provider is incomplete by design.** It selects up to three relevant
  sentences and skips table rows, so facts that live in tables or code blocks are often
  missed. That is the 0.603 expected-fact result; use it as the baseline a hosted model
  must beat.
- **Incremental ingest still rewrites the index file.** Chunking and embedding are
  skipped, but serialisation is O(index). Vectors belong in a store with partial updates
  before this design reaches tens of thousands of chunks.
- **PDF extraction is not OCR.** Scanned documents fail loudly; they do not silently
  produce an empty index. HTML extraction is heuristic and drops anything it considers
  chrome.
- **The index is single-node.** Collections isolate ids, not permissions: every valid API
  key can query every loaded collection. Real multi-tenancy needs per-key collection
  ACLs, per-tenant embedding budgets, a shared vector store, and a reindex queue.
- **Rate limits and usage are per process.** Multiple uvicorn workers will each allow the
  configured rate and write their own rows to SQLite. Use Redis/Postgres or a gateway
  before scaling horizontally.
- **Latency numbers are single-threaded** on the development machine. They compare
  configurations to each other, not to any production target.

---

## Project layout

```
src/cite/
  loaders/        Markdown/text, HTML, PDF → title + Markdown-ish body
  chunking.py     heading-aware, block-aware chunking
  ingest.py       manifest, incremental rebuild, atomic persistence
  retrieval.py    BM25 (inverted index), dense, RRF, reranker, persistence
  embeddings.py   hashing (offline) and OpenAI embedders
  providers.py    extractive mock, OpenAI, Anthropic
  answer.py       retrieval → generation → citation resolution
  evaluation.py   recall@k, MRR, nDCG@k, answer checks, Markdown rendering
  api.py          FastAPI, auth, rate limit, SQLite usage log, collections
  cli.py          ingest / ask / eval / serve / info
scripts/
  benchmark_ingest.py   full vs incremental measurement
eval/
  golden.yaml           hand-labelled queries with notes and expected facts
  results.md            latest retrieval + answer benchmark output
  ingest-benchmark.md   latest full-vs-incremental measurement
docs/
  operations.md         deployment checklist and multi-tenant gaps
data/corpus/            14-document product handbook used as the demo corpus
tests/                  97 tests: loaders, manifests, metrics, citations, hardening
```

## Tests

```bash
pytest            # 97 passed
ruff check src tests scripts
```

The suite covers the parts that are easy to get subtly wrong: nDCG against known values,
chunk overlap coverage, BM25 idf behaviour, citation marker bounds, PDF/HTML extraction,
manifest reuse and invalidation, vector round trips, auth, rate limiting, usage
aggregation, collection scoping, and the HTTP contract.

## Deployment

```bash
docker build -t cite .
docker run -p 8000:8000 cite
```

The image builds the frontend, installs the loader extras, and builds the index from
`data/corpus` at build time, so container start-up needs no ingestion step. Generated
indexes, collections, and the usage database are excluded by `.dockerignore`; the image
always indexes the committed corpus rather than a developer's local state. Replace
`data/corpus` before building to ship your own documents, and mount a volume over `data/`
if you want the SQLite usage log to survive a restart.

For anything beyond a demo, set `CITE_API_KEYS`, put the container behind TLS, and follow
[`docs/operations.md`](docs/operations.md). CI (`.github/workflows/ci.yml`) runs lint,
tests, the retrieval benchmark, and the ingestion benchmark.

---

MIT licensed. The corpus is synthetic documentation for a fictional product, written for
this project.
