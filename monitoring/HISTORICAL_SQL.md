# Freematics durable history contract

Prometheus is the live telemetry store. Its retention is bounded and it cannot
reconstruct capture-time samples after expiry. The Trips dashboard therefore
has a SQLite datasource seam with UID `freematics-history`.

The archive importer must expose a read-only SQLite database with these tables
and views. The current importer uses milliseconds for all timestamp columns so
it can retain both device capture time and collector receipt time:

```sql
CREATE TABLE IF NOT EXISTS trip (
    trip_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    archive_path TEXT NOT NULL UNIQUE,
    collector_login_ms INTEGER NOT NULL,
    start_capture_ms INTEGER,
    end_capture_ms INTEGER,
    timestamp_quality TEXT NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    data_bytes INTEGER NOT NULL DEFAULT 0,
    gap_count INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sample (
    trip_id TEXT NOT NULL REFERENCES trip(trip_id) ON DELETE CASCADE,
    device_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    device_monotonic_ms INTEGER NOT NULL,
    capture_utc_ms INTEGER,
    collector_received_ms INTEGER,
    timestamp_quality TEXT NOT NULL,
    latitude REAL,
    longitude REAL,
    gps_speed_kph REAL,
    gps_heading_degrees REAL,
    gps_hdop REAL,
    gps_satellites INTEGER,
    PRIMARY KEY (trip_id, sequence)
);

CREATE TABLE IF NOT EXISTS sample_metric (
    trip_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    pid TEXT NOT NULL,
    numeric_value REAL,
    text_value TEXT,
    PRIMARY KEY (trip_id, sequence, pid),
    FOREIGN KEY (trip_id, sequence) REFERENCES sample(trip_id, sequence) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS sample_trip_time ON sample(trip_id, capture_utc_ms);
CREATE INDEX IF NOT EXISTS sample_metric_pid ON sample_metric(trip_id, pid, sequence);
CREATE INDEX IF NOT EXISTS trip_device_time ON trip(device_id, start_capture_ms DESC);

CREATE VIEW IF NOT EXISTS metric_timeline AS
SELECT s.device_id, s.trip_id, s.sequence, s.device_monotonic_ms,
       s.capture_utc_ms, s.collector_received_ms, s.timestamp_quality,
       m.pid, m.numeric_value, m.text_value, s.latitude, s.longitude,
       s.gps_speed_kph, s.gps_heading_degrees, s.gps_hdop, s.gps_satellites
FROM sample AS s
JOIN sample_metric AS m USING (trip_id, sequence);
```

The current contract panel uses this Grafana SQLite query. Grafana's global
`$__from` and `$__to` variables are milliseconds, matching
`start_capture_ms`. `$device` and `$trip` are dashboard variables. The
all-trips value is `$__all`, matching the existing Prometheus selector.

```sql
SELECT trip_id AS "Trip",
       device_id AS "Vehicle",
       datetime(start_capture_ms / 1000, 'unixepoch') AS "Start",
       datetime(end_capture_ms / 1000, 'unixepoch') AS "End",
       sample_count AS "Samples",
       archive_path AS "Archive"
FROM trip
WHERE device_id = '$device'
  AND start_capture_ms BETWEEN CAST($__from AS INTEGER) AND CAST($__to AS INTEGER)
  AND ('$trip' = '$__all' OR trip_id = '$trip')
ORDER BY start_capture_ms DESC;
```

Until this importer and datasource are provisioned, the panel is intentionally
labelled as a pending durable archive seam. It must not be treated as proof
that historical data is currently available.
