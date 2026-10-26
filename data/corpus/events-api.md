# Events API

`POST /v2/events` is the only write path for product data. Everything else in
Northwind is derived from it.

## Request shape

```json
{
  "events": [
    {
      "event": "report_exported",
      "user_id": "u_42",
      "account_id": "acme",
      "timestamp": "2026-03-01T12:00:00Z",
      "properties": { "format": "csv", "rows": 1200 },
      "idempotency_key": "export-u_42-2026-03-01-1200"
    }
  ]
}
```

## Field rules

| Field | Required | Rules |
|---|---|---|
| `event` | yes | 1-64 characters, `snake_case` recommended, not enforced |
| `user_id` | yes | string, 1-128 characters, opaque |
| `account_id` | no | string, 1-128 characters; enables account reporting |
| `timestamp` | no | ISO 8601 with timezone; defaults to receipt time |
| `properties` | no | object, max 256 top-level keys, max depth 5 |
| `idempotency_key` | no | string, max 128 characters, honored for 24 hours |

Numeric IDs are rejected with `422`. Convert them to strings at the client. Boolean
and null property values are stored as-is and are filterable.

## Batch limits

A single request may contain up to **500 events** or **1 MB** of JSON, whichever
comes first. Requests above either limit are rejected with `413` and nothing in the
batch is written. The API does not partially apply a batch.

## Idempotency

Supply `idempotency_key` on every event that might be retried. Two events with the same
key inside a 24-hour window are collapsed into one; the second is counted in the
`duplicates` field of the response and is not billed. After 24 hours the key is
forgotten and a repeat is treated as new data.

Idempotency keys are scoped to the project, not the user. Reusing a key across two
different users is a bug; the second event is dropped.

## Backfill

Events may carry a timestamp up to **30 days** in the past. Older events are rejected
with `422` and code `timestamp_too_old`. Enterprise projects can request a 12-month
backfill window. Timestamps in the future are clamped to receipt time.

## Ordering

Northwind does not guarantee ordering. Late events are inserted by timestamp and
dashboards recompute affected windows, so a funnel built on a day that receives a
late event will change retroactively. If ordering matters for your use case, sort in
your own warehouse export instead.

## Response

```json
{
  "accepted": 500,
  "duplicates": 3,
  "rejected": 0,
  "request_id": "req_01H..."
}
```

A `202` with `rejected > 0` means the batch was partially valid. Rejected events are
returned by `GET /v2/events/rejections` for 24 hours.

## SDKs

Official SDKs exist for JavaScript, Python, Go, and Ruby. Each batches automatically,
retries on `429` and `5xx`, and sets an idempotency key per event. Use an SDK unless you
have a reason not to.
