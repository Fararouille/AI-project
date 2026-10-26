# Troubleshooting

Start with the symptom. Most tickets are one of the eight cases below.

## Events return 202 but never appear

Ingestion is asynchronous: an accepted event is queryable within **two minutes**,
guaranteed within ten. If nothing appears after ten minutes, check the following in
order:

1. Is the event in `GET /v2/events/rejections`? A `202` can still hide rejected events.
2. Is the key a test key? Test data is isolated and invisible to the live project.
3. Does the tile cache still hold an old result? Cached tile results expire after five
   minutes.
4. Is the timestamp more than 30 days old? Those events are rejected, not queued.

## Dashboards show the wrong day

All timestamps are stored and rendered in **UTC**. A dashboard filtered to a local day
does not match an export partitioned by UTC day. Convert at the presentation layer, or
use the `timezone` property on the account.

## Duplicate events

Duplicates mean either the client retried without an idempotency key, or the key expired
after 24 hours. Add `idempotency_key` and make it deterministic - derived from the
business event, not from a random UUID. Never derive it from a timestamp rounded to the
second, because retries land in different seconds.

## 401 invalid_key

The key is malformed, revoked, or from the other environment. Confirm the prefix
(`nw_live_` versus `nw_test_`) and that the header is `Authorization: Bearer <key>`, not
`X-API-Key`.

## 403 insufficient_scope

The key is valid but scoped wrong. Ingestion keys cannot read queries; read keys cannot
write events. Mint a second key rather than widening an existing one.

## 413 payload too large

A batch exceeded 500 events or 1 MB. Nothing in the batch was written. Split the batch
client-side; the SDKs do this automatically.

## 422 unprocessable

Almost always a type error: `user_id` sent as a number, a nested property deeper than
five levels, or a property with more than 256 top-level keys. The response includes the
offending field path and index.

## 429 too many requests

Honour `Retry-After`, back off with jitter, and batch more events per request. The
sustained ingestion limit is 600 requests per minute; batching 500 events per request
means you rarely approach it.

## Funnel numbers changed retroactively

Late events are inserted by timestamp, so historical windows recompute. This is expected
behaviour, not data loss. If a funnel must be stable, save it as a static cohort.

## Still stuck

Include the `request_id` from the response header and the UTC timestamp in any support
ticket. Without `request_id`, support cannot trace a request across services.
