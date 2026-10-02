# General condition monitoring

## Authorised scope and contract

Retain passive voltage and raw motion readings continuously, and compare the car
with its own earlier trips under matched operating conditions. These features
report observations and coverage, not a failed component or a healthy-car verdict.

The existing PID:value archive and its ordered `sample_field` projection remain
the source of truth. Add this version 1 contract to the firmware header and Python
catalogue before use. Each ordinary 250 ms frame can carry bounded repetitions:

| PID | Meaning | Encoding |
| --- | --- | --- |
| 0x0A0 | Passive voltage acquisition | unsigned milliseconds;centivolts |
| 0x0A1 | Motion acquisition timestamp | unsigned milliseconds |
| 0x0A2 | Raw accelerometer, including gravity | x;y;z, g, six decimal places |
| 0x0A3 | Gyroscope | x;y;z, degrees/second, six decimal places |
| 0x0A4 | Cumulative waveform losses since boot | voltage overflow;motion overflow;invalid voltage;invalid motion |
| 0x0A5 | Waveform format | integer 1 |

Motion fields form an adjacent 0x0A1, 0x0A2, 0x0A3 group. A decoder rejects an
incomplete or malformed group; it never pairs fields from different readings.
Absolute uint32 acquisition timestamps avoid dependence on frame formatting time.
Signed modular differences align timestamps across rollover. A negative offset
means a reading preceded the enclosing frame. The raw archive retains all fields;
ordinary latest-value metrics are unchanged.

Use separate 128-reading FIFO buffers for voltage and motion. Drain at most 16 of
each into a frame. At the nominal 50 Hz input, 64 readings/second drain capacity
permits recovery after a short delay. If full, reject the new reading and increase
its overflow counter; do not silently overwrite an older reading. Commit a FIFO
pop only after the complete group fits the destination frame. Keep pending data
when a RAM queue slot is unavailable. Reject non-finite or physically unencodable
values, with separate counters. A failed hardware read creates no held waveform.

Increase each queue slot from 2048 to 3072 bytes. Keep the configured slot count;
the PSRAM payload allocation grows from 2 MiB to 3 MiB. Actual allocation already
stops at available memory and must be checked on hardware. Full catalogue data
uses 2001 bytes; 16 voltage records (192 bytes), 16 motion groups (640 bytes),
loss counters (20 bytes) and format (5 bytes) total 2858 bytes. Each scalar/vector
field stays below the existing 256-byte formatting limit. The encoded complete
frame must fit the existing 8192-byte recorder limit, proved by the host path.

Passive voltage is an uncalibrated device input measurement. 50 Hz is an
acquisition target, not a measured guarantee or an alternator ripple test.
Continuous capture supplies pre/post-event history only while powered and while
finite storage can retain it. Upload bandwidth and archive growth increase.

## Failure cases, declared before implementation and isolated checks

- Lost/repeated/reordered acquisitions at drain boundaries, or after no free slot.
- Partial motion groups when destination RAM is full; pending data must survive.
- Sensor failure fabricates waveform points or stops the other sensor.
- Overflow or invalid sensor values disappear without a counter.
- Full catalogue plus waveform exceeds RAM or text limits, truncates arrays, or
  causes journal records to be discarded.
- Timestamp zero, uint32 rollover, acquisition after parent frame start, and
  delayed readings are interpreted as huge ages or as fresh observations.
- Upload failure/replay loses intermediate repeated fields or changes ordering.
- Collector latest-value projection hides raw waveform data from queries.
- Legacy frames without waveform fields falsely imply complete waveform coverage.
- Malformed, incomplete, unsupported-version or non-finite groups become readings.
- Analysis confuses telemetry loss with a vehicle fault or treats low-rate data as
  proof of sub-second sensor coverage.
- Wrap-up pauses ordinary collection while passive watcher tasks continue.
  Stop adding waveform readings in wrap-up, but drain pending acquisitions in
  bounded normal frames until empty. If no destination slot is free, retry at
  the next deadline. Preserve pending readings at the upload deadline rather
  than clearing them; their own clocks identify delayed acquisition.
- Symmetric vibration produces zero peak-to-peak if a decoder subtracts the
  smallest vector magnitude from the largest. Measure axis span instead.
- A loss counter resets within a query window and later rises beyond the first
  value. The observed net delta must remain unknown across that reset.
- Baseline includes target/newer trips, stale held values, unknown operating state,
  or too few distinct prior trips, and produces false confidence.

## Verification and delivery

Use real production buffer/serializer, journal and collector/indexer boundaries
with synthetic sensor data. Verify waveform timing, bounds, overflow, replay and
query results. Run the firmware build and focused existing regression checks.
Apply the similarity-checks skill to changed Python. Commit validated increments.
Hardware scheduler, SD power-loss behaviour, voltage calibration, actual sensor
cadence and physical deployment remain separate evidence requirements.
