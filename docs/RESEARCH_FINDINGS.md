# Freematics Model B research findings

Status: research and implementation recommendations. This document does not
claim that a recommendation is enabled until the vehicle test proves it.

## Evidence base

| Source | Relevant fact | Design consequence |
| --- | --- | --- |
| [Freematics ONE+ Model B product page](https://freematics.com/products/freematics-one-plus-model-b/) | The Model B has an ESP32, 8 MB PSRAM, ICM-42627 motion sensor, u-blox M9 GNSS, SIM7670 LTE Cat-1 modem, OBD access, CAN sniffing, and a buzzer. It supports CAN and KWP protocols. | The product can collect more than basic OBD Mode 01 data, but each data class needs its own controlled discovery process. |
| [Freematics Quick Start](https://freematics.com/pages/products/freematics-one-plus/quick-start-guide/) | TeleLogger reads OBD, motion, and GPS, and sends data through Wi-Fi or cellular. It states that APN and supported radio bands must be correct. | Keep cellular, server delivery, and OBD as separate health checks. An IP address does not prove a server POST. |
| [SIMCom A7670 documentation index](https://en.simcom.com/product/A7670X.html) | SIMCom publishes A76XX AT-command, HTTP(S), MQTT(S), FOTA, UART, and hardware documentation. | Use the SIM7670 HTTP(S) command path now. Assess MQTT only after its data and operational costs are measured. Do not add a second protocol only for presumed efficiency. |
| [ISO 15031-4 preview](https://cdn.standards.iteh.ai/samples/40087/4952be520aee4f50ab04b4a209e8554d/ISO-15031-4-2005.pdf) | Test equipment must not cause CAN bus failures. A no-response requires a retry, then a supported-PID check before further requests. | Poll only known supported PIDs. Treat ECU busy/no-response as a scheduling input, not as a metric value of zero. Maintain a bus-error budget and lower rate on errors. |
| [Upstream TeleLogger](https://github.com/stanleyhuangyc/Freematics/blob/master/firmware_v5/telelogger/telelogger.ino) | The upstream sketch targets Model A and B, initialises cellular separately, and runs telemetry asynchronously from main collection work. | Preserve that separation. Do not start a second concurrent OBD reader merely to obtain a nominal faster interval. |

## Current fork assessment

The current fork already uses good foundations:

* `obd_pids.h` holds the standard Mode 01 catalogue and labels.
* `COBD::isValidPID()` tests the ECU support map before the firmware polls a
  PID.
* Four driving values have priority 1: calculated load, RPM, vehicle speed,
  and throttle position.
* Priority-1 values use a 500 ms requested cadence. Other advertised PIDs
  rotate in groups of eight every five seconds.
* The upload path batches at most 24 complete samples. It waits at most five
  seconds for a normal batch and keeps the full batch for ordered retry after
  a failed request.
* The payload is compact text, not JSON. It contains hexadecimal PID keys,
  values, commas, and one checksum. It is efficient enough for the stated
  SIM cost before a binary protocol adds compatibility risk.
* Parked standby turns radios off, waits for MEMS motion, and performs the
  network ping-back at `PING_BACK_INTERVAL` (currently 15 minutes).

These are requested intervals, not hard real-time guarantees. `processOBD()`
performs sequential OBD requests. A slow ECU response can extend a collection
cycle. This is safer than overlapping requests through the same OBD bridge.

## What the collected vehicle evidence says

The vehicle has already advertised and returned useful values for engine load,
coolant temperature, fuel trims, MAP, RPM, speed, intake temperature, MAF,
throttle and pedal positions, fuel level, barometric pressure, catalyst
temperature, control-module voltage, equivalence ratio, and ambient
temperature. It did not advertise the standard engine fuel-rate PID `0x5E`.

This supports two rules:

1. Use direct ECU fuel rate when the ECU advertises it.
2. Label any MAF-derived fuel-rate and MPG calculation as an estimate. It
   needs the fuel type, ethanol content, and a chosen stoichiometric ratio.

The raw metrics panel must remain the source of truth. A missing PID means
"unsupported by this ECU" until a later support-map scan proves otherwise. It
does not mean zero or a vehicle fault.

## Recommended firmware changes, in priority order

### 1. Add measured OBD scheduling and back-pressure

Add per-request elapsed time, success, negative-response class, and timeout
counts. Export them as device metrics. Define a safe operating policy:

| Condition | Action |
| --- | --- |
| Priority-1 request completes below the budget | Keep the requested 500 ms cycle. |
| Two slow or failed priority-1 reads in one minute | Keep the core set, halve auxiliary work for five minutes. |
| Repeated bus errors or ECU busy responses | Stop auxiliary reads, run a support-map/protocol health check, and report degraded OBD state. |
| No OBD response while battery voltage exists | Report `obd_state=no_ecu_response`; do not create zero-value frames. |

This is more valuable than a 300 ms target. The vehicle ECU, its OBD protocol,
and the Freematics link set the true ceiling. The standard requires equipment
to avoid harmful bus effects.

### 2. Split PIDs into explicit collection classes

The current priority field should become a documented schedule with a cap per
cycle. Suggested initial classes are:

| Class | Period while moving | Examples | Purpose |
| --- | ---: | --- | --- |
| Drive | 500 ms | RPM, OBD speed, throttle, load, MAF, MAP | Trip reconstruction, acceleration, response to driver demand. |
| Powertrain | 2 s | coolant, intake air, timing advance, fuel trims, equivalence ratio, voltage | Thermal and fuelling trend detection. |
| Fuel and emissions | 5 s | fuel level, fuel pressure, rail pressure, catalyst temperature, O2/wideband sensors, EGR, EVAP | Economy estimates and degradation detection. |
| Inventory | start and then 60 s | VIN, OBD standard, fuel type, odometer, DTC state, MIL runtime | Vehicle context. Query only where support is confirmed. |

MAF and MAP should move into the drive class if the benchmark shows that the
core cycle remains below the bus budget. Their values directly improve fuel
and load analysis. Do not add them first without timing evidence.

### 3. Make the data contract self-describing

Transmit these durable context records at boot, OBD reconnect, and trip start:

* firmware version and Git commit;
* device ID, hardware type, IMEI hash or server-side identifier;
* OBD protocol and ECU support bitmap;
* VIN only if the server treats it as sensitive data;
* vehicle profile version, fuel type, engine displacement if known;
* current time source and time-valid flag;
* calibration versions for MEMS and derived fuel calculations.

This makes later MCP analysis reproducible. A rule engine must know whether a
fuel-rate is direct, estimated, stale, or absent.

### 4. Use a versioned binary transport only after a capture study

The present batch format is an appropriate first production protocol. It saves
far more cellular data than JSON because it avoids repeated field names and
amortises HTTPS headers over batches. It also works with the existing
collector.

Before changing it, capture ten normal journeys and measure bytes per sample,
bytes per request, request success rate, retry volume, and median upload age.
If the results justify more work, add a new content type such as
`application/x-freematics-v2` with:

* a version byte and schema ID;
* delta timestamps;
* varint PID IDs and fixed-point values where loss is acceptable;
* a CRC or authenticated transport checksum;
* explicit batch sequence number and acknowledgement;
* server support for both old and new formats during migration.

Do not send `Content-Encoding: gzip` until the modem command set and collector
support it end to end. Tiny batches can become larger after compression, and a
silent proxy decompression mismatch would lose data.

### 5. Improve low-power state reporting before changing thresholds

The 0.4 g threshold is a vector-magnitude movement test. The code subtracts a
stored accelerometer bias but does not yet distinguish a short shock from a
real drive. Add these records:

* standby entry and exit reason;
* wake score, consecutive above-threshold samples, and wake duration;
* ignition/voltage wake evidence where present;
* GNSS fix state and satellite quality at trip start;
* modem shutdown and reconnect reason.

Use a two-stage wake policy after real parked-car logs are available: a short
motion confirmation window, then an OBD ignition or speed check. This reduces
wind and vibration false wakes without delaying a real drive by 15 minutes.
MEMS motion can wake the device immediately; the 15-minute value only limits
the parked server ping-back.

### 6. Add CAN discovery as a separate, read-only feature

The Model B product supports CAN sniffing. It can reveal manufacturer-specific
signals that Mode 01 does not expose, but it is not a generic PID catalogue.
Implement it as a disabled-by-default capture mode with a time limit,
allow-listing, rate limit, privacy review, and raw frame retention limit.

Do not decode proprietary frames from guessed definitions. Record CAN ID,
payload, timing, and vehicle profile first. Later decoding needs a vehicle-
specific signal definition, measured correlation, and versioned provenance.

## Dashboard improvements that follow from the evidence

Prioritise decisions over more large number panels:

1. Add an "OBD quality" strip: protocol, support-map age, core PID latency,
   timeout rate, and last successful PID. This explains missing values.
2. Put direct and estimated fuel data in one panel with a clear source label.
   Show fuel-level rate-of-change only when the fuel-level signal is stable.
3. Add derived trend panels: coolant warm-up time, long-term trim drift,
   battery voltage under load, catalyst temperature distribution, and MAF
   versus RPM/load. Use baselines from the same vehicle, not generic healthy
   thresholds.
4. Add a diagnostic timeline for DTC transitions, MIL status, freeze-frame
   capture, OBD disconnects, GNSS loss, and cellular retry episodes.
5. Add data-quality markers to every trip: missing time, delayed uploads,
   source (OBD or GNSS speed), GPS HDOP, and measurement cadence.
6. Keep the raw ECU table as the complete metric record. Curated panels must
   link or identify the raw PIDs that generated their result.

## MCP mechanic architecture

Build this in layers. Do not begin with a free-form model that reads every raw
sample.

1. **Data API**: expose bounded queries for trips, time windows, a metric
   catalogue, DTC history, vehicle profile, and data quality. Return units,
   provenance, cadence, and direct-versus-derived flags.
2. **Feature service**: calculate stable trip and rolling features. Examples
   are cold-start warm-up time, RPM/load/MAF residuals, fuel-trim baseline
   drift, voltage sag, repeat DTC count, and route-normalised economy.
3. **Rules and anomaly service**: run explainable rules first. Each finding
   must include evidence, baseline, severity, uncertainty, and the exact raw
   series that supports it. Do not label a condition as a repair diagnosis.
4. **MCP tools**: provide `vehicle_context`, `trip_summary`,
   `metric_series`, `dtc_history`, `compare_baseline`, `data_quality`, and
   `explain_finding`. Enforce query limits and server-side aggregation.
5. **Assistant**: let the model assemble evidence into a readable report.
   It must state measurement gaps and recommend a professional inspection for
   safety-critical conditions.

Vehicle-specific context is required before strong conclusions. Store make,
model, model year, engine code, fuel type, transmission, known service work,
and expected use. A value that is unusual for one engine can be normal for
another.

## Validation gates

* Bench: confirm boot, accurate time, TLS verification, cellular attach, and
  server acknowledgement without OBD.
* Vehicle idle: record the OBD support map, protocol, direct values, and core
  PID request latency for five minutes.
* Controlled road trip: compare OBD speed and GNSS speed; confirm each
  schedule class, batch size, upload age, and retry behaviour.
* Parked test: measure standby current and false wakes for at least two
  overnight periods before changing the motion threshold.
* Analysis gate: no dashboard or MCP finding may hide unsupported data,
  stale data, estimated fuel values, or a low-quality GNSS position.
