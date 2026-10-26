# Alerts

Alerts watch a metric and notify a destination when it crosses a threshold or behaves
unusually. They are the operational layer on top of dashboards.

## Alert types

**Threshold alerts** fire when a metric crosses a fixed value. Example: signups drop
below 50 in a 24-hour window.

**Anomaly alerts** fire when a metric deviates from its expected value. The expectation
is a 7-day rolling baseline with weekly seasonality removed, and the trigger is a
z-score above 3 or below -3. Anomaly alerts need at least 14 days of history before
they produce useful signal; before that they stay in `warming_up`.

## Evaluation cadence

Alerts are evaluated every **15 minutes**, which is also the minimum evaluation window.
A 5-minute window is not available. When an alert fires, the notification includes the
metric, the observed value, the baseline, and a link to the dashboard.

## Destinations

- Email to any project member
- Slack via an incoming webhook URL
- Generic webhook, signed with HMAC-SHA256 (see `webhooks`)

A destination can be reused across alerts. Deleting a destination disables every alert
that points at it; the UI lists affected alerts before deletion.

## Muting and recovery

An alert can be muted for a fixed duration, up to **24 hours**. Muting suppresses
notifications but does not stop evaluation; the dashboard still records the incident.
Recovery notifications are sent once the metric returns inside its threshold for two
consecutive evaluations, which means up to 30 minutes after the metric actually
recovered.

## Avoiding noise

Three guidelines that matter more than any setting:

1. Alert on rates, not raw counts, when traffic is seasonal.
2. Use anomaly alerts for metrics that have a stable baseline and threshold alerts for
   metrics with a contractual target.
3. Route low-severity alerts to a Slack channel with a weekly review, not to a pager.

## Limits

- 100 alerts per project
- 20 destinations per project
- A single alert may not break down by more than one property

Alert history is retained for 90 days. Incidents older than that are not queryable and
cannot be exported.
