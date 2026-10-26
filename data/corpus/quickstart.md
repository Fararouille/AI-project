# Quickstart

This guide takes a new project from zero to a first dashboard in about ten minutes.

## 1. Create a project

A project is the top-level container for events, dashboards, and members. Most teams
create one project per environment, so `checkout-production` and `checkout-staging`
never mix data. Projects are created from the dashboard and cannot be renamed after
their first event is ingested.

## 2. Create an API key

Open **Settings > API keys** and create a key with the `ingest` scope. Copy it
immediately; the full value is shown once. Keys created in the test environment are
prefixed `nw_test_`, live keys are prefixed `nw_live_`.

Send the key as a bearer token:

```bash
curl -X POST https://api.northwind.dev/v2/events \
  -H "Authorization: Bearer nw_live_abc123" \
  -H "Content-Type: application/json" \
  -d '{"events":[{"event":"signup_completed","user_id":"u_42"}]}'
```

## 3. Send a first event

The minimum viable event has an `event` name and a `user_id`. Adding an `account_id`
unlocks account-level reporting, which is the point of using Northwind instead of a
page-view tool.

```json
{
  "events": [
    {
      "event": "signup_completed",
      "user_id": "u_42",
      "account_id": "acme",
      "timestamp": "2026-03-01T12:00:00Z",
      "properties": { "plan": "growth", "seats": 12 }
    }
  ]
}
```

A successful request returns `202 Accepted` with an `accepted` count. A `202` means
the batch was queued, not that it is queryable yet.

## 4. Build a dashboard

Open **Dashboards > New**, add an event trend tile, and select `signup_completed`.
Dashboards refresh at most once per minute, and tile queries are cached for five
minutes, so a freshly sent event can take up to five minutes to appear on a cached tile.

## 5. Invite the team

Members are invited by email and assigned one of three roles: `viewer`, `editor`, or
`admin`. Viewers can read dashboards but cannot create API keys. Seat changes are
billed on the next invoice.

## Common first-run mistakes

- Sending `user_id` as a number. IDs must be strings.
- Using a test key against the live API host.
- Forgetting `account_id`, then wondering why account filters are empty.
- Sending events with timestamps more than 30 days in the past.

If the first event does not appear, read `troubleshooting` before opening a ticket.
