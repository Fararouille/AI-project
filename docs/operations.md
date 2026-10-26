# Operations

This is the checklist for running `cite` as something other than a laptop demo, plus an
explicit list of what the current design does not solve.

## 1. Configuration

| variable | default | notes |
|---|---|---|
| `CITE_API_KEYS` | empty | Comma-separated. Empty means auth is **off**. |
| `CITE_RATE_LIMIT_PER_MINUTE` | `60` | Per identity. `0` disables. |
| `CITE_USAGE_DB` | `data/usage.db` | SQLite file. Mount a volume if it must survive restarts. |
| `CITE_LOG_QUESTIONS` | `false` | Off means only a SHA-256 prefix and question length are stored. |
| `CITE_CORPUS_DIR` | `data/corpus` | Used by `POST /api/reindex`. |
| `CITE_INDEX_PATH` | `data/index.json` | Default collection. |
| `CITE_COLLECTIONS_DIR` | `data/collections` | Named collections (`<name>.json`). |
| `CITE_COLLECTION` | `default` | Default collection when a request omits one. |
| `CITE_GOLDEN_PATH` | `eval/golden.yaml` | Evaluation questions. |
| `CITE_EMBEDDER` | `hashing` | `openai` requires `OPENAI_API_KEY`. |
| `CITE_LLM_PROVIDER` | `mock` | `openai` / `anthropic` require their keys. |

## 2. First deploy

1. Build the index with the same embedder the service will use:
   ```bash
   python -m cite --embedder openai ingest --corpus /data/handbook \
     --out /data/index.json --collection default
   ```
2. Set `CITE_API_KEYS` to a long random value, not `dev-secret`.
3. Put the process behind a TLS-terminating proxy. The app speaks plain HTTP.
4. Persist `/data/index.json`, `/data/collections/`, and `/data/usage.db`.
5. Run one process per container. The rate limiter and usage writer are process-local.
6. Check `GET /api/health` after start. `status` is `degraded` when no default index
   loaded; `auth_required` tells you whether keys are active.

## 3. Auth and identity

`X-API-Key: <key>` and `Authorization: Bearer <key>` are equivalent. Keys are compared
with `hmac.compare_digest`. The usage log stores `key:<sha256-prefix>`, never the key.

`/api/health` is public so container health checks work without credentials. Everything
else that can spend money or mutate state requires a key when `CITE_API_KEYS` is set:
`/api/ask`, `/api/ask/stream`, `/api/eval`, `/api/reindex`, `/api/usage`. `/api/sources`
is also behind the same dependency because it reveals document titles.

There are **no roles**: any valid key can reindex, run evaluation, and read usage. If you
need admin vs reader keys, add a role map keyed by the same identity hash before exposing
the service.

## 4. Rate limiting

A token bucket per identity, refilled at `CITE_RATE_LIMIT_PER_MINUTE / 60` tokens per
second, capacity equal to the per-minute value. A rejected request gets `429` plus
`Retry-After` in seconds.

Properties and limits:

- Identity is the API key when auth is on, otherwise the client IP.
- Buckets live in one process. Two replicas means two budgets.
- Buckets are pruned opportunistically above 4,096 identities; there is no background
  sweeper.
- The limiter protects the process, not the upstream LLM budget. Add a per-key daily
  spend cap before pointing it at a paid provider.

For a real deployment, replace `RateLimiter` with a Redis-backed counter and keep the
same `consume(identity) -> (allowed, retry_after)` shape.

## 5. Usage log

SQLite, WAL mode, one connection guarded by a lock. Each successful answer inserts:
timestamp, identity hash, collection, question hash, optional question text, question
length, retrieval mode, provider, latency, tokens in/out, cost, citation count.

`GET /api/usage?hours=24` returns:

- request count, average and p95 latency
- total tokens in/out and cost
- citation count
- breakdown by collection and retrieval mode
- timestamp of the last request

The log is append-only and never rotated by the app. Add retention (for example, delete
rows older than 30 days on a schedule) before it grows unbounded. It is not a metrics
system: there is no exporter and no per-request tracing. Ship structured logs or
OpenTelemetry separately if you need those.

`CITE_LOG_QUESTIONS=1` stores raw question text. Treat the database as sensitive if you
enable it, because user questions can contain customer data.

## 6. Collections

Each collection is a separate index file built from a corpus directory. There is no
cross-collection retrieval.

```bash
python -m cite ingest --corpus /data/handbook --collection handbook
python -m cite ingest --corpus /data/legal    --collection legal
```

The API loads every `*.json` in `CITE_COLLECTIONS_DIR` plus the default index at startup.
An unknown collection is a `404`; an invalid name is a `400`. A failed collection load is
logged and skipped rather than taking the whole service down.

Collections isolate ids, **not permissions**. Every valid key can query every collection.
Per-key ACLs belong in front of `_require_index`, using the identity returned by
`require_identity`.

`POST /api/reindex` re-reads `CITE_CORPUS_DIR`, not an arbitrary path. It is deliberately
impossible to point it at `/etc` through the request body. To reindex a different corpus,
run the CLI (or mount it at `CITE_CORPUS_DIR`).

## 7. Backups and recovery

- **Indexes**: reproducible from the corpus plus the embedder. Back them up only to avoid
  re-embedding cost. Keep the manifest and vectors together; a vector block whose length
  does not match the chunk list is discarded and recomputed on load.
- **Usage DB**: back up if the numbers matter. `sqlite3 data/usage.db ".backup ..."` is
  safe while the service is running.
- **Corpus**: the actual source of truth. Version it outside the container.

Atomic index writes mean a crash during `cite ingest` leaves the previous index intact.
A crash during a SQLite write leaves the transaction uncommitted; the log may lose the
last request, not corrupt earlier rows.

## 8. What is still missing

These are the honest gaps before calling this multi-tenant production software:

1. **Per-key collection ACLs and roles.** Any key can do anything today.
2. **Shared state.** Rate limits and usage are per process; indexes are loaded per
   process from local disk.
3. **A vector store with partial updates.** The whole index is rewritten on save.
4. **An ingestion queue.** `POST /api/reindex` is synchronous and holds a request open.
5. **OCR for scanned PDFs.** The loader fails loudly instead of indexing nothing.
6. **Cost controls.** There is a token/cost log but no per-key budget enforcement.
7. **Evaluation in CI against a hosted model.** The default harness uses the offline mock
   so CI stays free and deterministic.
