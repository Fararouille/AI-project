# Cohorts

A cohort is a saved group of users or accounts defined by behaviour. Cohorts are the
input to retention, funnels, and alert scoping.

## Static and dynamic cohorts

A **static** cohort is a frozen list, computed once when you save it. It is useful for
reproducing a historical analysis: the membership never changes, even as new events
arrive.

A **dynamic** cohort re-evaluates on a schedule. Membership changes as behaviour
changes, which is what you want for an operational segment like "accounts at risk".
Dynamic cohorts are recalculated every **6 hours**. A user who qualifies at 09:05
joins the cohort at the next recalculation, not immediately.

## Size limits

A cohort may contain at most **1,000,000 members**. Cohorts above that size are
clamped and flagged in the UI; the underlying query still runs, but segment filters
silently miss members, so keep segments narrow.

## Defining membership

Cohorts support four operators:

- `did` - performed an event, optionally with property constraints
- `did not` - never performed an event in the window
- `did at least N times` - frequency threshold
- `property is` - a stable user or account property

Membership can be combined with `and` / `or` to a depth of three. Deeper logic should
be expressed as a derived property instead, which is evaluated at ingestion and is
cheaper to query.

## Entity type

A cohort targets either users or accounts. Account cohorts use account-level
properties and are the right choice for B2B expansion questions. You cannot mix entity
types inside one cohort.

## Refreshing

Manual recalculation is available to `editor` and above. Recalculation is queued, not
synchronous: a large dynamic cohort can take up to 20 minutes. The UI shows the last
successful recalculation timestamp; if that timestamp is older than 12 hours, the
scheduler has fallen behind and the cohort should not be trusted for alerting.

## Deleting a cohort

Deleting a cohort does not delete the dashboards that reference it. Those tiles render
an error until the reference is removed. Check the **Used by** tab before deleting.
