-- Readers use mode=ro, with a writable directory for SQLite's WAL sidecars.
-- WAL keeps archive writes from blocking dashboard and analysis reads.
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS history_projection_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trip (
    device_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    archive_path TEXT NOT NULL UNIQUE,
    collector_login_ms INTEGER NOT NULL,
    start_capture_ms INTEGER,
    end_capture_ms INTEGER,
    timeline_start_ms INTEGER,
    timeline_end_ms INTEGER,
    time_basis TEXT NOT NULL DEFAULT 'unknown',
    timestamp_quality TEXT NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    data_bytes INTEGER NOT NULL DEFAULT 0,
    gap_count INTEGER NOT NULL DEFAULT 0,
    over_target_interval_count INTEGER NOT NULL DEFAULT 0,
    missing_capture_sequence_count INTEGER NOT NULL DEFAULT 0,
    gps_fix_count INTEGER NOT NULL DEFAULT 0,
    gps_poor_quality_count INTEGER NOT NULL DEFAULT 0,
    speed_disagreement_count INTEGER NOT NULL DEFAULT 0,
    archive_mtime_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (device_id, trip_id)
);

CREATE TABLE IF NOT EXISTS sample (
    device_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    device_monotonic_ms INTEGER NOT NULL,
    capture_utc_ms INTEGER,
    timeline_ms INTEGER,
    time_basis TEXT NOT NULL DEFAULT 'unknown',
    collector_received_ms INTEGER,
    archive_mtime_ms INTEGER NOT NULL,
    timestamp_quality TEXT NOT NULL,
    latitude REAL,
    longitude REAL,
    gps_speed_kph REAL,
    gps_heading_degrees REAL,
    gps_hdop REAL,
    gps_satellites INTEGER,
    acceleration_x_g REAL,
    acceleration_y_g REAL,
    acceleration_z_g REAL,
    capture_session_id TEXT,
    capture_sequence INTEGER,
    PRIMARY KEY (device_id, trip_id, sequence),
    FOREIGN KEY (device_id, trip_id) REFERENCES trip(device_id, trip_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sample_metric (
    device_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    pid TEXT NOT NULL,
    numeric_value REAL,
    text_value TEXT,
    PRIMARY KEY (device_id, trip_id, sequence, pid),
    FOREIGN KEY (device_id, trip_id, sequence) REFERENCES sample(device_id, trip_id, sequence) ON DELETE CASCADE
);

-- This append-only projection preserves duplicate PID fields in their source order.
-- sample_metric remains the convenient latest-value-per-PID projection.
CREATE TABLE IF NOT EXISTS sample_field (
    device_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    pid TEXT NOT NULL,
    numeric_value REAL,
    text_value TEXT,
    PRIMARY KEY (device_id, trip_id, sequence, ordinal),
    FOREIGN KEY (device_id, trip_id, sequence) REFERENCES sample(device_id, trip_id, sequence) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS sample_field_pid ON sample_field(device_id, trip_id, pid, sequence, ordinal);


-- Diagnostic rows are a decoded projection; the original count/slot fields
-- remain in sample_metric so malformed or unknown values are never discarded.
CREATE TABLE IF NOT EXISTS diagnostic_code (
    device_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    status TEXT NOT NULL,
    slot INTEGER NOT NULL,
    raw_code INTEGER NOT NULL,
    code TEXT NOT NULL,
    system TEXT NOT NULL,
    PRIMARY KEY (device_id, trip_id, sequence, status, slot),
    FOREIGN KEY (device_id, trip_id, sequence) REFERENCES sample(device_id, trip_id, sequence) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ingest_file (
    archive_path TEXT PRIMARY KEY,
    content_sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    processed_bytes INTEGER NOT NULL,
    sealed INTEGER NOT NULL DEFAULT 0,
    mutation_detected INTEGER NOT NULL DEFAULT 0,
    indexed_at_ms INTEGER NOT NULL
);

-- Capture identity is independent of archive enumeration and remains stable
-- across collector retries and uint32 capture-sequence wrap.
CREATE TABLE IF NOT EXISTS capture_inbox_record (
    device_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    capture_sequence INTEGER NOT NULL,
    payload_sha256 TEXT NOT NULL,
    source_path TEXT NOT NULL,
    PRIMARY KEY (device_id, session_id, capture_sequence)
);

CREATE TABLE IF NOT EXISTS metric_catalogue (
    pid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    unit TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 3,
    category TEXT NOT NULL DEFAULT 'mode01'
);

CREATE INDEX IF NOT EXISTS sample_trip_time ON sample(device_id, trip_id, timeline_ms);
CREATE UNIQUE INDEX IF NOT EXISTS sample_capture_identity
    ON sample(device_id, capture_session_id, capture_sequence)
    WHERE capture_session_id IS NOT NULL AND capture_sequence IS NOT NULL;
CREATE INDEX IF NOT EXISTS sample_metric_pid ON sample_metric(device_id, trip_id, pid, sequence);
CREATE INDEX IF NOT EXISTS diagnostic_trip_time ON diagnostic_code(device_id, trip_id, sequence);
CREATE INDEX IF NOT EXISTS trip_device_time ON trip(device_id, start_capture_ms DESC);
CREATE INDEX IF NOT EXISTS metric_catalogue_name ON metric_catalogue(name);

-- The indexer adds new nullable columns before rerunning this script; recreate views so
-- an existing projection sees the current evidence columns.
DROP VIEW IF EXISTS metric_timeline;
CREATE VIEW metric_timeline AS
SELECT
    s.device_id,
    s.trip_id,
    s.sequence,
    s.device_monotonic_ms,
    s.capture_utc_ms,
    s.timeline_ms,
    s.time_basis,
    s.collector_received_ms,
    s.timestamp_quality,
    s.capture_session_id,
    s.capture_sequence,
    m.pid,
    m.numeric_value,
    m.text_value,
    s.latitude,
    s.longitude,
    s.gps_speed_kph,
    s.gps_heading_degrees,
    s.gps_hdop,
    s.gps_satellites,
    s.acceleration_x_g,
    s.acceleration_y_g,
    s.acceleration_z_g
FROM sample AS s
JOIN sample_metric AS m
  ON m.device_id = s.device_id AND m.trip_id = s.trip_id AND m.sequence = s.sequence;

DROP VIEW IF EXISTS field_timeline;
CREATE VIEW IF NOT EXISTS field_timeline AS
SELECT
    s.device_id,
    s.trip_id,
    s.sequence,
    s.device_monotonic_ms,
    s.capture_utc_ms,
    s.timeline_ms,
    s.time_basis,
    s.timestamp_quality,
    s.capture_session_id,
    s.capture_sequence,
    f.ordinal,
    f.pid,
    f.numeric_value,
    f.text_value,
    s.latitude,
    s.longitude,
    s.gps_speed_kph,
    s.gps_heading_degrees,
    s.gps_hdop,
    s.gps_satellites,
    s.acceleration_x_g,
    s.acceleration_y_g,
    s.acceleration_z_g
FROM sample AS s
JOIN sample_field AS f
  ON f.device_id = s.device_id AND f.trip_id = s.trip_id AND f.sequence = s.sequence;

DROP VIEW IF EXISTS sample_gaps;
DROP VIEW IF EXISTS sample_capture_sequence_gaps;
DROP VIEW IF EXISTS sample_over_target_intervals;
DROP VIEW IF EXISTS sample_intervals;
CREATE VIEW IF NOT EXISTS sample_intervals AS
WITH ordered AS (
    SELECT
        device_id,
        trip_id,
        sequence,
        device_monotonic_ms,
        capture_utc_ms,
        timestamp_quality,
        LAG(sequence) OVER (PARTITION BY device_id, trip_id ORDER BY sequence) AS previous_sequence,
        LAG(device_monotonic_ms) OVER (PARTITION BY device_id, trip_id ORDER BY sequence) AS previous_device_monotonic_ms,
        LAG(capture_utc_ms) OVER (PARTITION BY device_id, trip_id ORDER BY sequence) AS previous_capture_utc_ms
    FROM sample
), deltas AS (
    SELECT
        *,
        (((((device_monotonic_ms - previous_device_monotonic_ms + 2147483648)
             % 4294967296) + 4294967296) % 4294967296) - 2147483648) AS gap_ms
    FROM ordered
)
SELECT
    device_id,
    trip_id,
    previous_sequence,
    sequence,
    previous_device_monotonic_ms,
    device_monotonic_ms,
    previous_capture_utc_ms,
    capture_utc_ms,
    gap_ms,
    timestamp_quality
FROM deltas
WHERE previous_device_monotonic_ms IS NOT NULL;

CREATE VIEW IF NOT EXISTS sample_gaps AS
SELECT * FROM sample_intervals WHERE gap_ms > 3000;

CREATE VIEW IF NOT EXISTS sample_over_target_intervals AS
SELECT * FROM sample_intervals WHERE gap_ms > 250;

CREATE VIEW IF NOT EXISTS sample_capture_sequence_gaps AS
WITH ordered AS (
    SELECT device_id, trip_id, capture_session_id, capture_sequence,
           LAG(capture_sequence) OVER (
               PARTITION BY device_id, capture_session_id ORDER BY sequence
           ) AS previous_capture_sequence
    FROM sample
    WHERE capture_session_id IS NOT NULL AND capture_sequence IS NOT NULL
), deltas AS (
    SELECT *, ((capture_sequence - previous_capture_sequence - 1 + 4294967296)
               % 4294967296) AS missing_sequences
    FROM ordered
)
SELECT device_id, trip_id, capture_session_id, previous_capture_sequence,
       capture_sequence, missing_sequences
FROM deltas
WHERE previous_capture_sequence IS NOT NULL AND missing_sequences > 0;
DROP VIEW IF EXISTS trip_metric_summary;

CREATE VIEW IF NOT EXISTS trip_metric_summary AS
SELECT
    m.device_id,
    m.trip_id,
    m.pid,
    c.name,
    c.description,
    c.unit,
    c.category,
    COUNT(m.numeric_value) AS sample_count,
    MIN(m.numeric_value) AS minimum,
    AVG(m.numeric_value) AS average,
    MAX(m.numeric_value) AS maximum,
    (SELECT latest.numeric_value
       FROM sample_metric AS latest
      WHERE latest.device_id = m.device_id AND latest.trip_id = m.trip_id AND latest.pid = m.pid
      ORDER BY latest.sequence DESC LIMIT 1) AS latest
FROM sample_metric AS m
LEFT JOIN metric_catalogue AS c ON c.pid = m.pid
GROUP BY m.device_id, m.trip_id, m.pid, c.name, c.description, c.unit, c.category;
