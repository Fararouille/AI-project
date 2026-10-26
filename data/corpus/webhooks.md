# Webhooks

Webhooks let Northwind push events to your service. They are used for alert delivery,
export completion notices, and membership changes.

## Delivery

Each delivery is an HTTP `POST` with a JSON body. Your endpoint must return a **2xx**
within **10 seconds**. Anything else, including a slow response, is treated as a failure
and retried.

## Retries

Failures retry five times with exponential backoff: **1 minute, 5 minutes, 30 minutes,
2 hours, and 6 hours**. If all five attempts fail the delivery is marked `dead` and
appears in the delivery log for 7 days. Dead deliveries are not retried automatically;
you can replay them manually from the dashboard.

Delivery is at-least-once, so handlers must be idempotent. Use the `delivery_id` in the
payload as your deduplication key.

## Signing

Every request carries `X-Northwind-Signature`, an HMAC-SHA256 of the raw request body
using the destination's signing secret. Verify it before parsing:

```python
import hmac, hashlib

expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
if not hmac.compare_digest(expected, request.headers["X-Northwind-Signature"]):
    return 401
```

Sign the **raw** body. Re-serialising parsed JSON changes key order and breaks the
signature, which is the most common integration bug.

## Payload

```json
{
  "delivery_id": "dlv_01H...",
  "type": "alert.triggered",
  "created_at": "2026-03-01T12:00:00Z",
  "project_id": "prj_123",
  "data": { "alert_id": "alt_9", "metric": "signups", "value": 12, "threshold": 50 }
}
```

Supported types are `alert.triggered`, `alert.recovered`, `export.completed`,
`export.failed`, `member.added`, and `member.removed`.

## Security

Only allow traffic from Northwind's published IP ranges and always verify the signature.
Do not put secrets in the URL path; the URL appears in logs. Rotate a signing secret
from the destination settings, which supports two active secrets for a 24-hour overlap.

## Testing

The dashboard can send a sample `alert.triggered` event to a destination. The sample is
signed with the real secret, so a handler that verifies signatures can be tested without
waiting for an incident.
