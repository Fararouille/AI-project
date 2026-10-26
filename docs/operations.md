# Operations

This is the checklist for running `cite` as something other than a laptop demo, plus an
explicit list of what the current design does not solve.

## 1. Configuration

| variable | default | notes |
|---|---|---|
| `CITE_API_KEYS` | empty | Comma-separated. Empty means auth is **off**. |
| `CITE_ADMIN_KEYS` | empty | Keys allowed to reindex, evaluate, and read usage. Should be a subset of `CITE_API_KEYS`. |
| `CITE_KEY_ACLS` | empty | `key:collection[,collection];key2:*`. `*` grants every collection. Keys cannot contain `:` or `;`. |
| `CITE_RATE_LIMIT_PER_MINUTE` | `60` | Per identity. `0` disables. |
| `CITE_RATE_LIMIT_BACKEND` | `sqlite` | `sqlite` shares one budget across workers on a host; `memory` is process-local. |
| `CITE_RATE_LIMIT_DB` | `data/rate-limit.db` | SQLite file for the shared token buckets. |
| `CITE_USAGE_DB` | `data/usage.db` | SQLite file. Mount a volume if it must survive restarts. |
| `CITE_LOG_QUESTIONS` | `false` | Off means only a SHA-256 prefix and question length are stored. |
| `CITE_CORPUS_DIR` | `data/corpus` | Used by `POST /api/reindex`. |
| `CITE_INDEX_PATH` | `data/index.sqlite3` | Default collection. `.json` keeps the original single-file store. |
| `CITE_COLLECTIONS_DIR` | `data/collections` | Named collections, `<name>.sqlite3` by default or `<name>.json` when the default is JSON. |
| `CITE_COLLECTION` | `default` | Default collection when a request omits one. |
| `CITE_UPLOADS_DIR` | `data/uploads` | Browser uploads, one subdirectory per collection. |
| `CITE_MAX_UPLOAD_MB` | `20` | Per-file upload cap; `0` disables uploads. |
| `CITE_GOLDEN_PATH` | `eval/golden.yaml` | Evaluation questions. |
| `CITE_EMBEDDER` | `hashing` | `openai` requires `OPENAI_API_KEY`. |
| `CITE_LLM_PROVIDER` | `mock` | `openai` / `anthropic` require their keys. |
| `CITE_JUDGE` | `lexical` | Answer-support judge: `lexical`, `openai`, or `anthropic`. |
| `CITE_PDF_OCR` | `auto` | Scanned PDFs: `auto` (empty text layers only), `always`, or `off`. |

The default install needs none of these. The controls are opt-in so a local clone keeps
working offline.

## 2. First deploy

1. Build the index with the same embedder the service will use. The default store is
   SQLite:
   ```bash
   python -m cite --embedder openai ingest --corpus /data/handbook \
     --out /data/index.sqlite3 --collection default
   ```
   Named collections are written to `CITE_COLLECTIONS_DIR` and inherit the default file
   suffix. Use `--out /data/index.json` only when a single diffable JSON file is wanted.
2. Set `CITE_API_KEYS` to long random values, not `dev-secret`. Split administrative keys
   with `CITE_ADMIN_KEYS` and grant collections with `CITE_KEY_ACLS`.
3. Put the process behind a TLS-terminating proxy. The app speaks plain HTTP.
4. Persist `/data/index.sqlite3`, `/data/collections/`, `/data/uploads/`,
   `/data/usage.db`, and `/data/rate-limit.db`.
5. Prefer one process per container, or several workers on one host if they share the same
   SQLite files. A rate-limit database on a different host per replica is not shared.
6. Check `GET /api/health` after start. `status` is `degraded` when no default index
   loaded; `auth_required`, `rate_limit_backend`, and `judge` report the active policy.

## 3. Auth, roles, and ACLs

`X-API-Key: <key>` and `Authorization: Bearer <key>` are equivalent. Keys are compared
with `hmac.compare_digest`. The usage log stores `key:<sha256-prefix>`, never the key.

`/api/health` is public so container health checks work without credentials. Everything
else that can spend money or mutate state requires a valid key when `CITE_API_KEYS` is
set: `/api/ask`, `/api/ask/stream`, `/api/eval`, `/api/reindex`, `/api/documents`
(POST and DELETE), `/api/usage`, `/api/me`, and `/api/sources`. `/api/sources` is included
because document titles can be sensitive.

Two optional maps turn the key list into a least-privilege policy:

```bash
export CITE_API_KEYS='reader-key,legal-key,admin-key'
export CITE_ADMIN_KEYS='admin-key'
export CITE_KEY_ACLS='reader-key:handbook,support;legal-key:legal;admin-key:*'
```

- **Admin keys** may `POST /api/reindex`, `POST /api/documents`, `DELETE
  /api/documents/{id}`, `POST /api/eval`, and `GET /api/usage`. They may also read every
  collection, regardless of an ACL entry.
- **Reader keys** may call `/api/ask`, `/api/ask/stream`, `/api/sources`, and `/api/me`,
  but only against their granted collections.
- **Unlisted keys** are still valid but have no collections and no admin rights. Requests
  for a collection return `403`.
- A `*` grant means every collection and still does **not** confer the admin role.
- `/api/me` reports the caller's role and the grants that are actually loaded, so a client
  can tell "not authenticated" from "not allowed".

Compatibility rule: when auth is enabled but **neither** `CITE_ADMIN_KEYS` nor
`CITE_KEY_ACLS` is set, every valid key keeps the original full-access behaviour. Setting
either map switches the whole service to least privilege. Configure both deliberately and
restart after changing either.

Auth disabled (empty `CITE_API_KEYS`) makes the local caller an admin with access to every
collection, because there is no boundary to protect. Do not expose that mode to a network.

## 4. Rate limiting

A token bucket per identity, refilled at `CITE_RATE_LIMIT_PER_MINUTE / 60` tokens per
second, capacity equal to the per-minute value. A rejected request gets `429` plus
`Retry-After` in seconds.

The default backend is SQLite:

- One row per identity in `CITE_RATE_LIMIT_DB`, updated inside `BEGIN IMMEDIATE`, so
  several workers on one host spend one budget instead of one each.
- Wall-clock time is used because `time.monotonic()` origins differ between processes.
  Clock jumps move every bucket together; this prevents abuse, it is not an accounting
  system.
- Rows older than an hour are pruned opportunistically. There is no background sweeper.
- If the file cannot be opened, construction logs a warning and falls back to the
  in-memory limiter instead of taking the API down.

`CITE_RATE_LIMIT_BACKEND=memory` preserves the old per-process behaviour and is useful in
tests or when one worker owns the process. Identity is the API key when auth is on and the
client IP otherwise.

Properties and limits:

- Replicas on different hosts each have their own database. Use Redis, a gateway, or a
  sticky single writer before scaling horizontally.
- The limiter protects request volume, not the upstream LLM budget. Add a per-key daily
  spend cap before pointing it at a paid provider.
- `0` disables limiting entirely.

## 5. Usage log

SQLite, WAL mode, one connection guarded by a lock. Each successful answer inserts:
timestamp, identity hash, collection, question hash, optional question text, question
length, retrieval mode, provider, latency, tokens in/out, cost, citation count.

`GET /api/usage?hours=24` is admin-only and returns:

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

At startup the API loads the default index plus every index file in
`CITE_COLLECTIONS_DIR`. A suffix that does not match the configured default is ignored
when both exist for the same name, so a migrated collection is not loaded twice. An
unknown collection is a `404`; an invalid name is a `400`; a collection the key cannot
read is a `403`. A failed collection load is logged and skipped rather than taking the
whole service down.

Collections isolate ids and, with `CITE_KEY_ACLS`, read permissions. They do **not** filter
documents inside a collection or enforce a per-key embedding budget. A key granted a
collection can read every document in it.

`POST /api/reindex` is admin-only and re-reads `CITE_CORPUS_DIR`, not an arbitrary path.
It is deliberately impossible to point it at `/etc` through the request body. It uses the
same manifest as the CLI and re-embeds only added or changed documents. To reindex a
different corpus, run the CLI or mount it at `CITE_CORPUS_DIR`; to reindex all collections,
call it once per collection name.

## 7. Storage, migration, and OCR

**SQLite is the default index store.** It has one row per document and one per chunk, with
vectors as float32 blobs. An incremental save deletes and reinserts rows only for added,
changed, and removed documents; the whole save is one transaction. That removes the
full-file rewrite the JSON store performs, not every O(corpus) cost:

- ingest still walks the corpus and hashes every document;
- BM25 is still rebuilt in memory when the index loads;
- one file means one writer, so concurrent reindex processes will serialise or time out.

**JSON remains supported.** `save_index` and `load_index` choose by suffix: `.json` for
JSON, `.sqlite3` / `.sqlite` / `.db` for SQLite. Named collections follow the default
suffix. Existing JSON deployments can set `CITE_INDEX_PATH=data/index.json` and keep
working unchanged.

**Migration.** On startup, if the configured SQLite index does not exist and a sibling
`<name>.json` does, the API loads the JSON index once and writes it to the configured
path. It logs `[cite] migrated ...`. A corrupt legacy file is logged and ignored; the API
then builds from the corpus if one is available. Re-running `cite ingest` is always the
explicit migration path.

**Scanned PDFs.** Install the optional OCR extra and the Tesseract binary:

```bash
pip install -e '.[ocr]'      # PyMuPDF + pytesseract + Pillow
# plus Tesseract: https://tesseract-ocr.github.io/tessdoc/Installation.html
```

- `CITE_PDF_OCR=auto` (default) OCRs only pages with an empty or near-empty text layer.
- `always` OCRs every page, useful when extraction is present but garbled.
- `off` never OCRs and raises a clear `no extractable text ... [ocr]` error on a scan.
- `cite ingest --ocr auto|always|off` overrides the environment for one run.

Without the Python packages, `auto` still fails loudly on a scanned page and `always`
raises a missing-dependency error naming the install command. Nothing is indexed as an
empty document. The Docker image installs the OCR extra and `tesseract-ocr`.

**Answer judging.** `CITE_JUDGE=lexical` is the offline default. `openai` and `anthropic`
use the matching provider to judge claim/evidence pairs and require that provider's API
key; `--judge` and `/api/eval?judge=` override the environment for one run. Evaluation
output always records the judge that ran. A model judge is not ground truth, is not
reproducible across model versions, and costs money per call.

## 8. Uploads

`POST /api/documents` accepts one multipart `file` field and indexes it into the default
collection. The bundled UI calls the same endpoint from **Corpus → add file** and then
scopes the next question to the new document id.

```bash
curl -s localhost:8000/api/documents -F "file=@notes/widgets.pdf" -H "X-API-Key: $ADMIN"
curl -s localhost:8000/api/ask -H "X-API-Key: $READER" -H 'content-type: application/json' \
  -d '{"question":"What is the widget limit?","doc_ids":["widgets"]}'
curl -s -X DELETE localhost:8000/api/documents/widgets -H "X-API-Key: $ADMIN"
```

Properties:

- **Admin-only.** Upload and delete use the same role check as `/api/reindex`. With
  `CITE_API_KEYS` empty, the local caller is an admin.
- **Outside the corpus.** Files are written to `CITE_UPLOADS_DIR/<collection>/`
  (`data/uploads/default/` by default), never to `CITE_CORPUS_DIR`. Extracted text is what
  gets indexed; the original file stays on disk for later rebuilds.
- **Layered, not mixed.** A rebuild reads the corpus first and the upload directory second,
  so a same-id upload shadows the corpus document. Deleting the upload restores the corpus
  version. Unchanged chunks and vectors are reused.
- **Name handling.** The browser-supplied name is reduced to its last path component, then
  to a slug plus a whitelisted suffix. `../../escape.md` becomes `escape.md` inside the
  upload directory. Re-uploading an id replaces the old file, including across extensions.
- **Validation.** The file must parse to non-empty text before it replaces anything, and
  the whole operation is rolled back if the rebuild fails. The same OCR rules apply as for
  `cite ingest`.
- **Size.** `CITE_MAX_UPLOAD_MB` defaults to 20 and `0` disables uploads. The limit is
  checked after Starlette spools the multipart body to disk, so a reverse proxy body limit
  is still the right place to reject huge requests early.
- **Synchronous.** Uploading embeds the new document and saves the index before responding.
  A large file under a hosted embedder can take seconds; do not expose this to untrusted
  users without a queue and a per-tenant quota.
- **Scoping.** `doc_ids` on `/api/ask` filters the candidate set before ranking. Unknown ids
  return `404`. The CLI equivalent is `cite ask "..." --doc widgets`.

Not covered: virus scanning, per-user quotas, document-level permissions inside a
collection, and uploads into named collections (the endpoint targets the default collection;
use `cite ingest --collection ...` for the others).

## 9. Backups and recovery

- **Indexes**: reproducible from the corpus plus the embedder. Back them up only to avoid
  re-embedding cost. For SQLite, use `sqlite3 data/index.sqlite3 ".backup ..."` while the
  service is idle or stopped; copying a live WAL database without its `-wal` and `-shm`
  files can lose recent writes. For JSON, a plain copy is complete because writes are
  atomic `os.replace` swaps.
- **Usage and rate-limit DBs**: back up if the numbers matter. `sqlite3 data/usage.db
  ".backup ..."` is safe while the service is running.
- **Corpus and uploads**: the actual source of truth. Version the corpus outside the
  container and back up `data/uploads/`; neither can be regenerated from the index.
  Indexes can always be rebuilt from them.

SQLite index saves run inside one `BEGIN IMMEDIATE` transaction, so a crash leaves the
previous committed state rather than a half-written index. JSON writes go to a temporary
file and are swapped in with `os.replace`. Uploads are written to a temporary same-suffix
file, parsed, and only then swapped into place; a failed rebuild restores the previous
bytes. A crash during a usage write can lose the last request, not corrupt earlier rows. A
schema-version mismatch is reported explicitly; re-ingest to migrate rather than editing
the file by hand.

## 10. What is still missing

These are the honest gaps before calling this multi-tenant production software:

1. **Cross-host shared state.** Rate limits and usage share one SQLite file per host;
   replicas on different hosts do not. Indexes are loaded from local disk per process.
2. **Document-level authorization.** ACLs stop at the collection. There is no per-document
   filter, redaction, or tenant namespace inside a collection, so a reader granted a
   collection can ask about every uploaded file in it.
3. **A vector store with concurrent writers.** SQLite removes the full rewrite and gives
   per-document upserts, but one file still has one writer and BM25 is rebuilt in memory.
4. **An ingestion queue.** `POST /api/reindex` and uploads are synchronous and hold a
   request open. There is no virus scanning, resumable upload, or per-user file quota.
5. **Cost controls.** There is a token/cost log but no per-key or per-tenant budget
   enforcement in the request path.
6. **Evaluation in CI against a hosted model.** The default harness uses the offline mock
   and lexical judge so CI stays free and deterministic. A hosted judge or provider run is
   manual.
7. **OCR coverage.** OCR quality depends on scan quality and installed language data; it
   is not a document-layout parser and is slow for large scan sets.
