# Rate Limits and Quotas

Northwind applies rate limits per project, per endpoint class. Limits exist to keep
one noisy integration from degrading query latency for everyone else.

## Limits

| Endpoint class | Sustained limit | Burst |
|---|---|---|
| Event ingestion (`POST /v2/events`) | 600 requests / minute | 2x for 10 seconds |
| Query endpoints (`GET /v2/query/*`) | 120 requests / minute | 2x for 10 seconds |
| Key management | 30 requests / minute | none |
| Export jobs | 10 requests / minute | none |

Limits are measured in **requests**, not events. Because a single ingestion request can
carry up to 500 events, an integration batching properly can move 300,000 events per
minute within the sustained limit.

## Headers

Every response includes:

- `X-RateLimit-Limit` - the sustained limit for this endpoint class.
- `X-RateLimit-Remaining` - requests left in the current window.
- `X-RateLimit-Reset` - Unix timestamp when the window resets.

## Handling 429

When a limit is exceeded the API returns `429 Too Many Requests` with a
`Retry-After` header in seconds. Clients must honour it. Recommended backoff is
exponential with jitter, starting at the `Retry-After` value, capped at 60 seconds, and
retrying up to five times.

Do not retry a `429` immediately; that converts a short throttle into a longer one.
Repeated bursts inside a one-minute window extend the window.

## Quotas versus limits

A **limit** is per-minute and resets automatically. A **quota** is per-month and does
not. The Growth plan includes 1,000,000 events per month; beyond that, overage is
billed per `billing`, not rejected. Ingestion is never blocked by quota exhaustion on
paid plans. On the Free plan, ingestion is blocked for the remainder of the month once
the 100,000-event quota is reached, and the API returns `402 Payment Required`.

## Per-key limits

If you need isolation between tenants, mint one key per tenant. Limits are still
enforced at the project level, so per-key isolation does not multiply your limit; it
only improves attribution in the dashboard.

## Raise a limit

Sustained ingestion above 600 requests per minute requires an Enterprise plan. Contact
support with your expected batch size and peak concurrency. Query limits can be raised
on Growth with a written justification.
