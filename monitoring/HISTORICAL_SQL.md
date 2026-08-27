# Freematics durable history contract

Prometheus is the live telemetry store. Its retention is bounded and it cannot
reconstruct capture-time samples after expiry. The Trips dashboard therefore
has a SQLite datasource seam with UID `freematics-history`.

## Current schema

The source of truth is [`collector/history_schema.sql`](../collector/history_schema.sql).
The indexer is [`collector/history_indexer.py`](../collector/history_indexer.py).
The database is a rebuildable projection of the raw `data/<device>/.../*.txt`
files. The raw files remain canonical.

The current schema uses these boundaries:

* `trip` has the composite key `(device_id, trip_id)`. It stores collector
  login time, optional GNSS capture bounds, a display timeline, timestamp
  quality, sample and gap counts, and the source archive path.
* `sample` has the composite key `(device_id, trip_id, sequence)`. It stores
  the device monotonic clock, optional `capture_utc_ms`, `timeline_ms`,
  `time_basis`, timestamp quality, GNSS coordinates, speed, heading, HDOP,
  satellite count, and nullable MEMS acceleration components.
* `sample_metric` stores numeric and text values for each PID in a sample.
  The PID is normalised as `0xNNN`, with standard Mode 01 values in the
  `0x100` range and device fields in their original range.
* `sample_field` and `field_timeline` preserve every PID occurrence in source
  order. `sample_metric` remains the one-row-per-PID projection for aggregate
  queries, so duplicate fields must use the occurrence-preserving view.
* `diagnostic_code` is a decoded view of stored, pending, and permanent DTC
  count/slot fields. It retains status, slot, raw uint16 code, formatted code,
  system, and sample time. It does not replace the raw fields.
* `metric_catalogue`, `metric_timeline`, `sample_gaps`, and
  `trip_metric_summary` provide metadata and bounded query surfaces.
* `ingest_file` records content hashes, processed size, sealing state, and
  mutation detection so an index pass is idempotent.

`capture_utc_ms` is populated only from valid GNSS date/time fields or a
monotonic interpolation anchored by valid GNSS. If capture UTC is unknown,
`timeline_ms` uses a session-relative display position and `time_basis` is
`collector_session`. This makes a row navigable without presenting the
collector login time as the vehicle's capture time. Dashboard labels must
show the basis and quality.

The indexer treats an archive as sealed after its modification time has been
quiet for the configured sealing interval. This is an operational boundary,
not proof that the vehicle produced a final frame. An active file holds its
final incomplete PID-0 frame until a later frame or sealing pass makes it
safe to project.

## Dashboard query contract

Grafana's global `$__from` and `$__to` values are milliseconds. `$device` and
`$trip` are SQL-escaped dashboard variables. Historical panels must filter
`timeline_ms`, not `capture_utc_ms`, so legacy rows with unknown capture UTC
remain inspectable. They must show `capture_utc_ms`, `timeline_ms`,
`time_basis`, and timestamp quality when the distinction matters.

Trip lists should use the display timeline and include trips with an unknown
capture timestamp:

```sql
SELECT trip_id AS "Trip",
       device_id AS "Vehicle",
       CASE WHEN timeline_start_ms IS NULL THEN 'Unknown display time'
            ELSE datetime(timeline_start_ms / 1000, 'unixepoch') END AS "Start",
       CASE WHEN timeline_end_ms IS NULL THEN 'Unknown display time'
            ELSE datetime(timeline_end_ms / 1000, 'unixepoch') END AS "End",
       timestamp_quality AS "Timestamp quality",
       time_basis AS "Display time basis",
       sample_count AS "Samples",
       gap_count AS "Gaps",
       archive_path AS "Archive"
FROM trip
WHERE device_id = '$device'
  AND timeline_start_ms BETWEEN CAST($__from AS INTEGER) AND CAST($__to AS INTEGER)
ORDER BY timeline_start_ms DESC;
```

Metric aggregates must join `sample_metric` to `sample` before applying the
time range. A `MAX(numeric_value)` is not a latest value. Use the highest
sequence in the selected range when a panel says `Latest`, and use `MIN`,
`AVG`, or `MAX` only when the panel names that aggregate.

Do not turn absent PIDs into zero. Keep `numeric_value` NULL for non-numeric
or unavailable fields, retain `text_value` for vectors and codes, and show
unsupported or stale data as unavailable. Derived fuel rate and economy must
identify their source and assumptions.

The database datasource is intentionally read-only. The indexer may rebuild
the projection from raw archives, but a dashboard query must not mutate
files, generate KML, or update ingest state. Any migration from an older
schema must create a verified backup and rebuild the projection; do not rely
on `CREATE TABLE IF NOT EXISTS` to change an existing table.
