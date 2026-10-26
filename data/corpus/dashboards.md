# Dashboards

A dashboard is a grid of tiles. Each tile is a saved query rendered as a chart, a
number, or a table.

## Limits

- 50 tiles per dashboard
- 200 dashboards per project
- A tile may not query more than 12 months of data on the Growth plan

Above 50 tiles the editor stops accepting new tiles rather than silently degrading
performance. Split the dashboard instead.

## Freshness and caching

Tile results are cached for **five minutes**. A dashboard cannot be configured to
refresh faster than **60 seconds**, and the UI polls at whatever interval the cache
allows. If a tile looks stale after an event was accepted, wait five minutes before
suspecting a bug.

Clearing the cache is an `admin` action available from the tile menu. It does not
re-run every tile; it invalidates them lazily on next view.

## Tiles

| Type | Best for | Notes |
|---|---|---|
| Trend | events over time | supports breakdown by up to 3 properties |
| Funnel | ordered step conversion | see `funnels` |
| Retention | repeat usage over time | weekly or monthly buckets |
| Number | a single KPI | optional comparison to prior period |
| Table | grouped breakdowns | max 1,000 rows rendered, export for more |

## Filters

Dashboard-level filters apply to every tile that opts in. A tile opts in by declaring
the filter key in its query. Filters are not automatically inherited: this is
deliberate, because a global filter that silently changes a KPI is worse than no filter.

Common filter keys are `plan`, `account_id`, `region`, and `is_internal`. The
`is_internal` filter is applied by default to exclude employee accounts.

## Sharing

Dashboards can be shared in three ways: with project members, with a signed public link,
or embedded in an iframe. Public links are read-only, expire after 30 days by default,
and never expose raw event properties - only aggregated values.

## Performance

Query cost grows with the number of breakdown dimensions, not with the number of tiles.
A dashboard with 40 tiles that each break down by one property is usually cheaper than
four tiles that break down by four properties. If a dashboard times out, reduce
breakdown cardinality first.
