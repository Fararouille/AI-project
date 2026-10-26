# Authentication

Every request to the Northwind API must carry a bearer token. There are no cookies,
no query-string keys, and no anonymous endpoints other than the health check.

## Key format

API keys have the shape `nw_<environment>_<24 base62 characters>`. For example,
`nw_live_9fQ2x...`. The environment segment is either `live` or `test`. Test keys
write to an isolated store that is wiped every 30 days and never billed.

## Scopes

A key carries exactly one scope, chosen at creation:

| Scope | Can do | Cannot do |
|---|---|---|
| `ingest` | write events | read events, manage project |
| `read` | query events, read dashboards | write events, manage keys |
| `admin` | everything, including key rotation | nothing |

Create separate keys per service. A browser client should never hold an `admin` key.

## Rotation

Rotate a key from **Settings > API keys > Rotate**. Rotation issues a new secret and
keeps the old secret valid for a 24-hour grace period so you can deploy without
downtime. After the grace period the old secret returns `401`.

Rotation does not change the key ID. Audit logs reference the ID, so rotating does not
break historical attribution.

## Failure modes

- `401 Unauthorized` with code `invalid_key` - the key is malformed or revoked.
- `401 Unauthorized` with code `key_expired` - the 24-hour rotation grace period ended.
- `403 Forbidden` with code `insufficient_scope` - the key is valid but lacks the scope
  for this endpoint. Ingestion keys cannot read, by design.

## Storing keys

Keys are shown once at creation and never again. Northwind stores only a salted hash,
so a lost key must be rotated rather than recovered. Do not commit keys; use your
platform's secret manager. If a key is leaked, rotate it first and investigate second -
the grace period is short enough that a leak is urgent.

## Service accounts

For machine-to-machine integrations, create a service account and issue it a key.
Service accounts do not consume a seat and do not receive email. They are the
recommended pattern for warehouse connectors and CI pipelines.

## Programmatic key management

`admin` keys can call `POST /v2/keys` to mint scoped keys, which is intended for
platforms that provision a key per tenant. Rate limits for key management are 30
requests per minute, separate from data-plane limits.
