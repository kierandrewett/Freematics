# Freematics durable history contract

Prometheus is the live telemetry store. Its retention is bounded and it cannot
reconstruct capture-time samples after expiry. The Trips dashboard therefore
has a SQLite datasource seam with UID `freematics-history`.

The archive importer must expose a read-only SQLite database with these tables
and views. It uses milliseconds for timestamp columns so it can retain the
device capture clock and an archive-file receipt approximation separately.
`capture_utc_ms` is populated only from valid GNSS date/time fields (or a
monotonic interpolation explicitly marked `anchored`). Old archives without a
GNSS date remain `unknown` and are not placed on a fabricated UTC timeline:

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

CREATE TABLE IF NOT EXISTS metric_catalogue (
    pid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    unit TEXT NOT NULL,
    priority INTEGER NOT NULL,
    category TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS sample_trip_time ON sample(trip_id, capture_utc_ms);
CREATE INDEX IF NOT EXISTS sample_metric_pid ON sample_metric(trip_id, pid, sequence);
CREATE INDEX IF NOT EXISTS trip_device_time ON trip(device_id, start_capture_ms DESC);
CREATE INDEX IF NOT EXISTS metric_catalogue_name ON metric_catalogue(name);

CREATE VIEW IF NOT EXISTS metric_timeline AS
SELECT s.device_id, s.trip_id, s.sequence, s.device_monotonic_ms,
       s.capture_utc_ms, s.collector_received_ms, s.timestamp_quality,
       m.pid, m.numeric_value, m.text_value, s.latitude, s.longitude,
       s.gps_speed_kph, s.gps_heading_degrees, s.gps_hdop, s.gps_satellites
FROM sample AS s
JOIN sample_metric AS m USING (trip_id, sequence);

CREATE VIEW IF NOT EXISTS sample_gaps AS
WITH ordered AS (
  SELECT trip_id, sequence, device_monotonic_ms, capture_utc_ms,
         timestamp_quality,
         LAG(sequence) OVER (PARTITION BY trip_id ORDER BY sequence) AS previous_sequence,
         LAG(device_monotonic_ms) OVER (PARTITION BY trip_id ORDER BY sequence) AS previous_device_monotonic_ms,
         LAG(capture_utc_ms) OVER (PARTITION BY trip_id ORDER BY sequence) AS previous_capture_utc_ms
  FROM sample
)
SELECT *, device_monotonic_ms - previous_device_monotonic_ms AS gap_ms
FROM ordered
WHERE previous_device_monotonic_ms IS NOT NULL
  AND device_monotonic_ms - previous_device_monotonic_ms > 3000;

CREATE VIEW IF NOT EXISTS trip_metric_summary AS
SELECT m.trip_id, m.pid, c.name, c.description, c.unit, c.category,
       COUNT(m.numeric_value) AS sample_count,
       MIN(m.numeric_value) AS minimum, AVG(m.numeric_value) AS average,
       MAX(m.numeric_value) AS maximum
FROM sample_metric AS m
LEFT JOIN metric_catalogue AS c ON c.pid = m.pid
GROUP BY m.trip_id, m.pid, c.name, c.description, c.unit, c.category;
```

The current contract panel uses this Grafana SQLite query. Grafana's global
`$__from` and `$__to` variables are milliseconds, matching
`start_capture_ms`. `$device` and `$trip` are dashboard variables. The
all-trips value is `$__all`, matching the existing Prometheus selector.

```sql
SELECT trip_id AS "Trip",
       device_id AS "Vehicle",
       CASE WHEN start_capture_ms IS NULL THEN 'Unknown capture time'
            ELSE datetime(start_capture_ms / 1000, 'unixepoch') END AS "Start",
       CASE WHEN end_capture_ms IS NULL THEN 'Unknown capture time'
            ELSE datetime(end_capture_ms / 1000, 'unixepoch') END AS "End",
       timestamp_quality AS "Timestamp quality",
       sample_count AS "Samples",
       archive_path AS "Archive"
FROM trip
WHERE device_id = '$device'
  AND (start_capture_ms BETWEEN CAST($__from AS INTEGER) AND CAST($__to AS INTEGER)
       OR (start_capture_ms IS NULL AND collector_login_ms BETWEEN CAST($__from AS INTEGER) AND CAST($__to AS INTEGER)))
  AND ('$trip' = '$__all' OR trip_id = '$trip')
ORDER BY start_capture_ms DESC;
```

Until this importer and datasource are provisioned, the panel is intentionally
labelled as a pending durable archive seam. It must not be treated as proof
that historical data is currently available.
