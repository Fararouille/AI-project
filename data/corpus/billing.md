# Billing

Northwind bills monthly in arrears. The invoice covers events, seats, and add-ons used
during the billing period.

## Plans

| Plan | Base price | Included events | Seats included |
|---|---|---|---|
| Free | $0 | 100,000 / month | 3 |
| Growth | $0 base | 1,000,000 / month | none |
| Enterprise | Custom | Custom | Custom |

Growth has no platform fee; you pay for events above the included amount and for seats.

## Event pricing

Overage on Growth is **$0.12 per 1,000 events**, billed to the nearest thousand. A
project at 1,400,000 events in a month pays for 400,000 overage events, or $48.00.

Only **accepted, non-duplicate** events are billed. Events rejected with `422` or `413`
are free. Duplicates deduplicated by idempotency key are free.

## Seats

A seat is any member who is not a service account. Seats are **$29 per seat per month**
on Growth. Viewers count as seats; service accounts do not. Removing a member mid-month
credits the unused portion on the next invoice.

## Proration

Upgrades are prorated to the day. Downgrades take effect at the start of the next
period and are not refunded. Annual commitments receive a **20% discount** and are
invoiced upfront; unused annual credit does not roll over.

## Quota behavior

On **Free**, ingestion returns `402 Payment Required` once the monthly quota is
exhausted, and metrics stop updating. On **Growth**, ingestion is never blocked by
quota; overage is billed. This is the most common reason a free project goes quiet.

## Payment and invoices

Invoices are issued on the first day of the following month and charged to the card on
file. Invoices older than 24 months are archived and must be requested from support.
Failed payments retry three times over seven days before the project is downgraded to
Free.

## Cost controls

Set a monthly spend cap in **Billing > Limits**. At 80% of the cap the project admins
receive an email; at 100% ingestion continues but an admin must acknowledge the cap
within 48 hours or the project is paused. Caps are available on Growth and Enterprise.
