# Freematics Model B car test runbook

This is the portable hand-off for the Freematics ONE+ Model B installed as
device `ZKUCALJ0`. The last recorded device-side image was fork commit
`12dc207bc559`, based on an earlier hardware observation. That historical
record does not verify the image currently installed after the interrupted
flash attempt. The source tree release marker is `1.0.0`, but use the
production rebuild and boot verification below before calling it installed.
Bench USB checks can verify boot, time, TLS, cellular attach and server
responses. They cannot verify vehicle facts.

## Known-good configuration

- Hardware: Freematics ONE+ Model B (`TYPE:14`)
- Modem: SIMCom SIM7670E-LN
- SIM provider: Simbase UK
- APN: `simbase`
- Network order: cellular first, Wi-Fi fallback
- Transport: authenticated HTTPS to `freematics.drewett.dev:443`, with CA, hostname and time verification
- Device ID: `ZKUCALJ0`
- Storage: internal SPIFFS; no SD card is required
- SIM orientation: insert it as shown in the Freematics Model B product image, with the electrical contacts facing up

The final USB test registered on operator `23415`, received a cellular IP, enabled strict TLS verification, authenticated, and received repeated `HTTP 200` responses from the collector. Live samples are grouped for up to five seconds and outage backlogs drain in complete, ordered batches of up to 24 samples per request.

## Light, buzzer and standby behaviour

- Online: the LED is on only while a real HTTPS telemetry request is in flight. One flash represents one request, which may contain several samples.
- Initial network search: slow repeating flash.
- Reconnecting after a previously healthy link: rapid flash.
- Parked standby: LED fully off; radios are shut down, the accelerometer is in low-power mode, and the ESP32 sleeps between motion checks. Tracking is paused; one parked marker is sent when standby begins. This is intentional standby, not an outage.
- Network offline for 15 seconds while working: the production image records the outage silently. The optional host-side notifier sends the event to the `freematics-device` ntfy topic.
- Network restored after an announced outage: the production image records recovery silently and leaves notification to the host-side notifier.

On the bench, this image enters standby after approximately 180 seconds without motion. It requires three consecutive 250 ms samples above the parked `0.5 g` threshold before restarting the active collection path, filtering out a single bump or vibration. Modem/GNSS/OBD startup and network registration still take additional time. There is no periodic cellular tracking while parked.

## Six-month unattended-use boundary

This firmware is not certified for six months connected directly to a vehicle battery. The published Model B low-power floor is approximately 10 mA with radios and GPS off; that is about 44 Ah over six months before the vehicle's own parasitic load, and the standby motion-monitoring loop may draw more. Use a measured current budget plus a switched or low-voltage-cutoff OBD supply before leaving it installed for months. A microSD card is optional for normal connected operation but required if a long network outage must be retained locally: the in-memory queue holds only 1,024 readings (about 8.5 minutes at the 500 ms moving cadence), while internal SPIFFS is a bounded rotating log rather than a six-month archive.

## Move the unit to the car

1. Turn the ignition fully off.
2. Unplug the Freematics unit from USB.
3. Insert it firmly into the car's OBD-II socket.
4. Turn the ignition on without starting the engine.
5. Leave it powered for 90 seconds while it discovers the ECU, reads the VIN and supported PIDs, starts the stored/pending/permanent fault-code scan rotation, acquires GNSS, and registers on LTE.
6. Check the device at [Freematics Admin](https://freematics-admin.drewett.dev/) or open the [Grafana vehicle dashboard](https://grafana.drewett.dev/d/freematics-vehicle?var-device=ZKUCALJ0).
7. Once ECU data is visible, start the engine and leave it idling for two minutes. This adds live RPM, load, temperatures, fuel/air readings, voltage, and other ECU-advertised values to the inventory.

After a reflash, capture the first serial boot lines, including `[BOOT] Release:`
and `[BOOT] Build:`. A clean build prints the short Git revision; a build made
with uncommitted changes is suffixed with `-dirty`. Record both values alongside
the flash date so the installed image can be distinguished from later source
commits.

The unit sends data autonomously over the SIM. A laptop is only needed if live serial logs are required for diagnosis.

## What is collected

- Every numeric standard Mode 01 PID that the ECU advertises as supported, with a complete raw metric table as well as friendly names
- Bounded fast OBD polling for core driving metrics, with at most two priority-1 PIDs per 500 ms slice
- All other supported standard PIDs in a bus-safe rotating poll
- Stored Mode 03, pending Mode 07, and permanent Mode 0A diagnostic trouble codes
- VIN, battery voltage, device temperature, LTE/Wi-Fi signal, and connection state
- GNSS latitude, longitude, altitude, heading, satellites, HDOP, and speed
- Three-axis acceleration and device orientation where available
- Trip identity, duration, and GPS-first distance with OBD-speed fallback
- A raw per-trip server archive so future dashboards and decoders can revisit the original readings
- A dedicated two-second Prometheus scrape with bounded 400-day/10 GB retention, feeding the history-first Grafana trip index, route, summaries, diagnostics and exhaustive raw-metric table

Manufacturer-specific Mode 22 PIDs are not universally discoverable or decodable. They require the make/model/year and an appropriate definition set. The standard inventory above is deliberately exhaustive without guessing proprietary commands.

## OBD quality and vendor discovery

The firmware now emits these device fields with every active sample:

| Field | Meaning |
| --- | --- |
| `0x085` | OBD bridge protocol number from `ATDPN`, or zero when unknown |
| `0x086` | Count of standard Mode 01 PIDs advertised by the ECU |
| `0x087` | Cumulative OBD read failures for the active firmware session |
| `0x088` | Slowest OBD response in the latest collection cycle, in milliseconds |
| `0x089` | OBD state: `0` disconnected, `1` ready, `2` degraded |
| `0x08A` | Consecutive failed core OBD cycles |
| `0x08B` | Filled telemetry readings waiting for upload |
| `0x08C` | Encoded telemetry bytes waiting for upload |
| `0x310` | Stored DTC read state: `0` no response, `1` response, `2` codes |
| `0x330` | Pending DTC read state: `0` no response, `1` response, `2` codes |
| `0x350` | Permanent DTC read state: `0` no response, `1` response, `2` codes |

Use these fields to explain missing values. Do not treat a failed read as a
zero measurement.

The queue fields report the backlog before the current sample enters the
firmware queue. A later sample reflects any change caused by that sample.

For Corsa-specific discovery, collect the VIN, engine code, ECU response
address, bus activity, bitrate evidence, and raw response before enabling a
profile candidate. Start with passive capture and advertised standard PIDs.
Only a known read-only identifier with a positive support response may proceed
to decoding. Unknown identifiers and negative responses must be recorded and
backed off. Do not send session-control, security-access, write, clear-fault,
coding, or actuator requests.

`COBD::readReadOnlyService` is a bounded firmware seam for a confirmed
`0x1A` GMLAN or `0x22` UDS response. The TeleLogger does not call it by
default. A caller must first apply the profile registry and then pass the raw
response to `collector/vendor_diagnostics.py`; this keeps candidate discovery
separate from standard polling.

For a bounded passive CAN evidence run, set `ENABLE_CAN_CAPTURE` to `1` in
`config.h` for a temporary evidence build. The capture runs once after the
device finds a CAN OBD protocol, lasts at most 30 seconds, stores at most 512
raw monitor lines, and never calls a CAN transmit function. Each capture writes
an adjacent timestamp field (`0x000`) and hexadecimal raw monitor line
(`0x092`). The raw monitor line is capped at 120 bytes to keep each local
record bounded. Review the raw line offline before adding a Corsa-specific
profile. Restore `ENABLE_CAN_CAPTURE` to `0` and rebuild the release image
after the evidence run.

Analyse a copied archive without connecting to the vehicle:

```bash
python3 -m venv /tmp/freematics-gmlan-venv
/tmp/freematics-gmlan-venv/bin/python -m pip install -r tools/requirements-gmlan.txt
PYTHONPATH=collector /tmp/freematics-gmlan-venv/bin/python collector/gmlan_capture.py \
  --format archive /path/to/capture.txt > gmlan-evidence.json
```

The report records the source hash, ISO-TP payload, CAN identifier, positive or
negative capability result, and matching profile candidate. It does not change
the profile or send a request. If the archive format is not an ELM-style
monitor line, the record is counted as unparsed evidence. Keep the original
archive beside the report.

The repository tests use synthetic captures for parser-contract coverage. They
are not vehicle evidence.

## Optional laptop serial check

Use a verified checkout of this repository. Do not clone the default branch
and treat its build as the production image:

```bash
cd /path/to/freematics
git status --short --branch
env -u FREEMATICS_TOKEN -u PRODUCTION_BUILD pio run -e esp32dev
pio device monitor --port /dev/ttyUSB0 --baud 115200
```

The `env -u` prefixes keep credentials and production mode out of this bench
build even when a production token is exported in the shell. This command is a
bench build only and is not release evidence. Use the production rebuild command
in the next section before flashing a vehicle device.

Current source-emitted milestones include:

```text
[BOOT] Release: 1.0.0
[BOOT] Build: <short source commit>
[BOOT] Device ID: ZKUCALJ0
[BOOT] Hardware type: 14
[OBD] ECU connected
VIN:<vehicle VIN>
CELL:SIM7670E-LN
APN:simbase
[CELL] In service
[HTTP] Server accepted <N> values
```

The OBD and HTTP lines depend on the connected hardware and server response.
The batch count varies. The number in `[HTTP] Server accepted` is the count of
decoded values, not the count of requests.

`[OBD] ECU not available` means the ECU is not awake, the unit is not seated in
the OBD socket, or the vehicle/protocol has not responded. It does not indicate
a SIM or server failure.

## Rebuilding or reflashing

The committed `local_config.h.example` documents configuration, but Wi-Fi and
telemetry credentials are intentionally not committed. The collector is gated
by Caddy on both public and private ingress, so production firmware must be
built with the device's 64-character bearer token obtained through the
authorised server-secret process. Keep the committed production cadence:

* `OBD_FAST_INTERVAL_MS=500UL`, with two priority-1 PIDs per cycle
* `OBD_AUX_INTERVAL_MS=5000UL`, with eight rotating auxiliary PIDs per cycle
* `STANDBY_POLL_INTERVAL_MS=250UL`

Build the image, record its hash, then flash it. Before running this block,
manually confirm that `local_config.h` contains the intended deployment server
and APN without printing credentials:

```bash
set -eu
test -f local_config.h || { printf '%s\n' 'local_config.h is required'; exit 1; }
device_token="${FREEMATICS_TOKEN:-}"
test "${#device_token}" -eq 64 || { printf '%s\n' 'FREEMATICS_TOKEN must contain 64 hexadecimal characters'; exit 1; }
case "$device_token" in
  *[!0123456789abcdefABCDEF]*) printf '%s\n' 'FREEMATICS_TOKEN contains a non-hexadecimal character'; exit 1 ;;
esac
PRODUCTION_BUILD=1 FREEMATICS_TOKEN="$device_token" pio run -e esp32dev
sha256sum .pio/build/esp32dev/firmware.bin
PRODUCTION_BUILD=1 FREEMATICS_TOKEN="$device_token" pio run -e esp32dev -t upload --upload-port /dev/ttyUSB0
pio device monitor --port /dev/ttyUSB0 --baud 115200
```

`FREEMATICS_TOKEN` must be exactly 64 hexadecimal characters. When
`PRODUCTION_BUILD=1` is set, `build_secrets.py` stops before creating the image
if the token is missing or malformed. The ignored `local_config.h` must also
contain this deployment's server and APN settings; without it, the firmware
falls back to the upstream UDP endpoint and an empty APN. Stop before flashing
when either input is absent. Do not use a bench-only build or a placeholder
token as an installable fallback. Never paste the token into Git, shell
history, screenshots or support logs. Do not clear diagnostic codes from the
firmware or dashboard during initial testing. Reading codes is non-destructive;
clearing them can erase useful freeze-frame evidence and readiness state.
