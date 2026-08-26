# Live data analysis: ZKUCALJ0

## Scope and method

This is an observation report. It uses read-only Prometheus API queries against
`https://prom.drewett.dev`. It does not infer vehicle health from a short test
drive. Timestamps in this document are UTC.

* Analysis query time: `2026-08-26T17:14:07Z` to `2026-08-26T17:15:48Z`.
* Device: `ZKUCALJ0`.
* Query window for the driving data: `2026-08-26T16:00:00Z` to
  `2026-08-26T17:15:00Z`, with a five-second query step.
* The query step is Prometheus scrape resolution. It is not proof of the
  device sample interval.

## Direct observations

### Vehicle data

The collector currently exposes 33 distinct standard Mode 01 PID values. The
shared catalogue contains 87 values. This is a good result for the vehicle,
but it is not all values in the catalogue. An ECU can reject optional PIDs.
The dashboard must show rejected PIDs as `not supported`, not as zero.

The useful observed PID set includes engine speed, vehicle speed, engine load,
coolant temperature, intake temperature and pressure, mass air flow, short-
and long-term fuel trim, throttle and pedal positions, fuel level, oxygen
sensor voltages, catalyst temperature, barometric pressure, evaporative purge,
control-module voltage, and diagnostic status counters.

The strongest continuous driving segment was trip `20260826-161629`:

| Observation | Evidence |
| --- | --- |
| Time span with changing OBD values | `16:19:20Z` to `16:43:05Z` |
| Engine speed | 774.5 to 4,154 rpm (`pid="0x10C"`) |
| OBD speed | 0 to 85 km/h (`pid="0x10D"`) |
| GPS speed | 0 to 84.7 km/h |
| Coolant temperature | 39 to 94 C (`pid="0x105"`) |
| Mass air flow | 2.32 to 39.31 g/s (`pid="0x110"`) |
| Fuel level | 14.90 to 14.12 percent (`pid="0x12F"`) |
| Short-term fuel trim | -3.12 to 3.91 percent (`pid="0x106"`) |
| Long-term fuel trim | 0.78 to 6.25 percent (`pid="0x107"`) |
| Control-module voltage | 14.08 to 14.35 V (`pid="0x142"`) |
| Catalyst temperature B1S1 | up to 756 C (`pid="0x13C"`) |
| Stored, pending and permanent DTC counts | all zero |

The fuel estimate has an important limit. The ECU does not expose PID `0x5E`
(`engine_fuel_rate`), fuel type, oil temperature, torque, rail pressure, or
odometer. The dashboard can calculate an estimate from mass air flow, but it
must label it as an estimate. For a petrol engine, using 14.7:1 air-fuel ratio
and 745 g/L fuel density, moving samples in this segment estimated 2.74 to
45.01 UK mpg, with a simple sample mean of 6.65 UK mpg. That mean is not a
trip economy value because it includes low-speed samples and uses an assumed
fuel model. It is useful only as a live load/consumption indicator.

The trim range is modest in this short sample. That does not identify a fault.
The voltage range is consistent with a charging system while the engine runs.
The catalyst value is high but can be normal under load. The system needs
baselines over many warm drives before it can identify a change.

### GPS quality

GPS satellite count was 12, but HDOP was 5.8 to 19.8 during the observed
period. This is poor positional precision. Satellite count alone is not a
quality measure. The map and trip-distance calculations must expose GPS
quality and avoid high-confidence route or harsh-event conclusions when HDOP
is above a configured threshold.

### Transport and freshness

Prometheus recorded these transport transitions in the analysis window:

| Time | `freematics_device_connected` |
| --- | --- |
| 16:16:30Z | 1 |
| 16:31:00Z | 0 |
| 16:43:30Z | 1 |
| 16:53:00Z | 0 |
| 17:10:30Z | 1 |
| 17:13:30Z | 0 |

At `17:14:07Z`, the current collector values were `connected=0`, data age
`30.432` seconds, reported sample rate `3.392/min`, and cellular transport
value `2`. The received-byte counter reset to zero at `17:10:30Z`, then moved
from 452 bytes at `17:11:00Z` to 2,321 bytes at `17:13:30Z`.

This confirms that compact buffered delivery occurs. It does not yet prove a
stable cellular session. The intermittent connection state explains the
offline indication. The collector needs separate transport state, delivery
lag, queued-sample count, and last-successful-upload time. A single
`connected` tile is too ambiguous.

### Trip integrity defect

Prometheus contains these trip-labelled OBD series:

| Trip ID | OBD value range in query result |
| --- | --- |
| `20260826-161629` | 16:19:20Z to 16:43:05Z; values changed while driving |
| `20260826-164309` | 16:43:10Z to 16:49:50Z; RPM held at 3,735.75 and speed at 76 km/h |
| `20260826-164953` | 16:49:55Z to 17:10:25Z; the same RPM and speed values persisted |
| `20260826-171027` | 17:10:30Z to 17:14:45Z; the same values persisted |

The last three series are not credible live vehicle readings. GPS speed fell
to about 0.2 to 1.7 km/h after `16:43:10Z`, while OBD speed remained 76 km/h.
The device was subsequently on the bench. This is stale OBD state copied into
new transport sessions or trip labels. It is the highest-priority data
correctness problem.

## Exact Prometheus queries used

```promql
count(count by (pid) (freematics_obd_value{device_id="ZKUCALJ0"}))
freematics_device_connected{device_id="ZKUCALJ0"}
freematics_device_data_age_seconds{device_id="ZKUCALJ0"}
freematics_device_sample_rate_per_minute{device_id="ZKUCALJ0"}
freematics_device_data_received_bytes_total{device_id="ZKUCALJ0"}
freematics_trip_active{device_id="ZKUCALJ0"}
freematics_diagnostic_trouble_codes{device_id="ZKUCALJ0"}
freematics_obd_value{device_id="ZKUCALJ0",pid="0x10C"}
freematics_obd_value{device_id="ZKUCALJ0",pid="0x10D"}
freematics_obd_value{device_id="ZKUCALJ0",pid="0x12F"}
freematics_obd_value{device_id="ZKUCALJ0",pid="0x110"}
freematics_gps_speed_kilometres_per_hour{device_id="ZKUCALJ0"}
freematics_gps_hdop{device_id="ZKUCALJ0"}
```

The fuel estimate query was:

```promql
(
  freematics_obd_value{device_id="ZKUCALJ0",pid="0x10D"} * 0.621371
)
/
on(device_id,trip_id)
(
  freematics_obd_value{device_id="ZKUCALJ0",pid="0x110"} * 3600 / (14.7 * 745)
)
```

## Recommended work order

1. Fix stale-state handling before adding more dashboard panels. Add an OBD
   session generation and per-PID source timestamp. Clear or mark every OBD
   value invalid when OBD disconnects, the ignition is off, or the device
   starts a new trip. The collector must only export a PID when the current
   session received that PID.
2. Decouple trip lifecycle from network lifecycle. A modem reconnect or a
   device reboot must not create a driving trip. Start a trip from a sustained
   ignition/OBD-ready state and end it after sustained ignition-off or
   stationary timeout. Keep a separate boot/session identifier for diagnosis.
3. Export transport observability. Add `last_upload_success`, queued samples,
   queued bytes, batch size, HTTP status, retry count, cellular registration
   state, and upload latency. This will distinguish a true offline device from
   a healthy device that is batching data.
4. Keep fast polling for RPM, vehicle speed, load and throttle. Poll changing
   auxiliary values at a slower rate. Record each PID source time so the UI
   can show actual age rather than imply that all values are fresh.
5. Add a data-quality rail to the dashboard: OBD active/inactive, source age
   by metric group, GPS HDOP state, transport state, backlog, and a clear
   stale-data banner. Suppress route segments and harsh-driving events when
   the source data is stale or GPS quality is poor.
6. Add historical health baselines, not fixed fault claims. Use cooldown-aware
   baselines for coolant warm-up time, long-term fuel trim, control-module
   voltage, MAF versus RPM/load, catalyst temperature, and fuel-level trend.
   Flag a change from the vehicle's own normal range for review.
7. Maintain a capability profile per vehicle. Record supported and rejected
   PIDs after several sessions. Use it to select panels, show unavailable
   information plainly, and stop spending bus time on unsupported PIDs.

## Requirements for the future diagnostic MCP server

The MCP server should consume a normalised trip and signal API, not raw
Prometheus values alone. Each signal needs value, unit, source timestamp,
quality, PID, OBD session, trip ID, and transport receipt time. It should use
the vehicle capability profile, fuel type, engine type, model year, and
service history before it produces advice. It must return evidence, confidence,
alternative explanations, and the next observation to collect. It must label
early warnings as monitoring findings, not confirmed mechanical diagnoses.
