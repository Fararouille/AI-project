# Northwind Analytics Overview

Northwind Analytics is a product analytics platform for B2B SaaS teams. It answers
questions about how accounts adopt, retain, and expand by collecting product events,
grouping them into accounts, and letting teams explore them in dashboards, funnels,
and cohorts.

## What Northwind is for

Northwind is built for teams that sell to companies rather than consumers. Every event
is tied to both a **user** and an **account**, so a question like "which accounts
activated last week but have not invited a teammate" is a single query instead of a
join across three tools.

The product has four surfaces:

- **Ingestion** - an HTTP events API and a set of SDKs.
- **Exploration** - dashboards, funnels, retention curves, and cohorts.
- **Monitoring** - threshold and anomaly alerts with Slack and webhook delivery.
- **Export** - scheduled Parquet exports to your own object storage.

## How data flows

1. Your client, server, or warehouse connector sends events to `POST /v2/events`.
2. Events are validated, deduplicated, and written to the ingestion queue.
3. A worker resolves identity, applies account stitching, and writes to the columnar store.
4. Queries run against the columnar store; dashboards cache results for five minutes.

Ingestion is asynchronous. An event is usually queryable within two minutes of a
successful `202` response, and the platform guarantees queryability within ten minutes.

## Plans at a glance

| Plan | Retention | Included events | SSO |
|---|---|---|---|
| Free | 30 days | 100,000 / month | No |
| Growth | 12 months | 1,000,000 / month | Add-on |
| Enterprise | Custom | Custom | Included |

Retention applies to raw event data. Aggregated dashboard results are retained for the
life of the project.

## What Northwind is not

Northwind is not a session replay tool, a warehouse, or a marketing automation suite.
It does not store personally identifying information by design: user IDs and account
IDs are opaque strings, and the API rejects fields that look like email addresses in
event properties unless you explicitly enable PII collection on the project.

## Related documentation

- Getting started: `quickstart`
- Authenticating requests: `authentication`
- Sending events at volume: `events-api`
- Understanding request limits: `rate-limits`
