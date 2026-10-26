# Data Export

Export moves raw events into your own warehouse. It is the escape hatch for anything
Northwind's query layer cannot express.

## Destinations

Supported destinations are Amazon S3 and Google Cloud Storage. Each destination stores:

- bucket and prefix
- region
- a write-only credential
- a schema version

A project may have at most **5 destinations**. Destinations are write-only by design:
Northwind never reads from your bucket, so a leaked credential limits damage to the
prefix you configured.

## Format

Exports are **Parquet**, one file per table per run:

- `events/` - one row per accepted event
- `users/` - current user identity state
- `accounts/` - current account state

Partitions are `dt=YYYY-MM-DD`. Files are immutable and never rewritten; a correction
arrives as a new file, so consumers should deduplicate on `event_id`.

## Schedule

Exports run daily at **02:00 UTC**. A run covers the previous complete UTC day. Runs
are idempotent by partition: re-running a day replaces nothing and appends a file with
a `run_id` suffix, so consumers must handle multiple files per partition.

The first export after enabling a destination performs a **90-day backfill**. Backfill
runs in the background and can take several hours for large projects. Events older than
90 days are not included in the backfill.

## Retention and delivery guarantees

Export delivery is at-least-once. A file may appear twice after a retry. Delivery is not
guaranteed for deleted events: if an event is deleted for a data-subject request, later
exports omit it, but files already written are not rewritten.

## Troubleshooting a failed run

1. Check the destination's IAM policy grants `s3:PutObject` on the prefix.
2. Confirm the bucket region matches the destination region.
3. Look at the export log for a `403`; that is almost always a permission issue.
4. If the run shows `partial`, some partitions succeeded. Do not re-enable and disable
   the destination to force a retry; use the manual re-run action instead.
