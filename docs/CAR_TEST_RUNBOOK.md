# Freematics Model B car test runbook

This is the portable hand-off for the Freematics ONE+ Model B installed as device `ZKUCALJ0`. The device is currently running the previously validated, token-bearing production image from source commit `b39c9f76074a09f14f2118ff4f739ceea197d7ca` and does not need a laptop to send telemetry. The focused fork now contains later OBD-freshness, silent-buzzer, and quiet-standby changes; those changes have been compiled and pushed but were not flashed during this readiness check. Treat the quiet-standby behaviour below as **source-only until a reflash and serial proof are recorded**.

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
- Parked standby (new fork image, after reflash): LED fully off; radios are shut down, the accelerometer is in low-power mode, and the ESP32 sleeps between motion checks. This is intentional standby, not an outage. The currently installed image may still use the older standby indication.
- Network offline for 15 seconds while working: the current flashed image records the outage and may emit its older audible alert. The newer fork image disables routine buzzer alerts and must be flashed before relying on silent operation. The optional host-side notifier sends the event to the `freematics-device` ntfy topic.
- Network restored after an announced outage: the current flashed image may emit its older restore tone; the newer fork image records recovery silently and leaves notification to the host-side notifier.

On the bench, the new fork image enters standby after approximately 180 seconds without motion. It detects motion within approximately 250 ms above the configured `0.4 g` motion threshold, then restarts the active collection path; modem/GNSS/OBD startup and network registration still take additional time. While parked it otherwise performs a cellular ping-back every 15 minutes. The currently installed image must be reflashed before relying on this behaviour.

## Six-month unattended-use boundary

This firmware is not certified for six months connected directly to a vehicle battery. The published Model B low-power floor is approximately 10 mA with radios and GPS off; that is about 44 Ah over six months before the vehicle's own parasitic load, and the standby motion-monitoring loop may draw more. Use a measured current budget plus a switched or low-voltage-cutoff OBD supply before leaving it installed for months. A microSD card is optional for normal connected operation but required if a long network outage must be retained locally: the in-memory queue holds only 1,024 readings (about 8.5 minutes at the 500 ms moving cadence), while internal SPIFFS is a bounded rotating log rather than a six-month archive.

## Move the unit to the car

1. Turn the ignition fully off.
2. Unplug the Freematics unit from USB.
3. Insert it firmly into the car's OBD-II socket.
4. Turn the ignition on without starting the engine.
5. Leave it powered for 90 seconds while it discovers the ECU, reads the VIN and supported PIDs, scans stored/pending/permanent fault codes, acquires GNSS, and registers on LTE.
6. Check the device at [Freematics Admin](https://freematics-admin.drewett.dev/) or open the [Grafana vehicle dashboard](https://grafana.drewett.dev/d/freematics-vehicle?var-device=ZKUCALJ0).
7. Once ECU data is visible, start the engine and leave it idling for two minutes. This adds live RPM, load, temperatures, fuel/air readings, voltage, and other ECU-advertised values to the inventory.

After a reflash, capture the first serial boot lines, including `[BOOT] Build:`. A clean build prints the short Git revision; a build made with uncommitted changes is suffixed with `-dirty`. Record that value alongside the flash date so the installed image can be distinguished from later source commits.

The unit sends data autonomously over the SIM. A laptop is only needed if live serial logs are required for diagnosis.

## What is collected

- Every numeric standard Mode 01 PID that the ECU advertises as supported, with a complete raw metric table as well as friendly names
- Core values every cycle: vehicle speed, engine RPM, calculated load, and throttle
- All other supported standard PIDs in a bus-safe rotating poll
- Stored Mode 03, pending Mode 07, and permanent Mode 0A diagnostic trouble codes
- VIN, battery voltage, device temperature, LTE/Wi-Fi signal, and connection state
- GNSS latitude, longitude, altitude, heading, satellites, HDOP, and speed
- Three-axis acceleration and device orientation where available
- Trip identity, duration, and GPS-first distance with OBD-speed fallback
- A raw per-trip server archive so future dashboards and decoders can revisit the original readings
- A dedicated two-second Prometheus scrape with bounded 400-day/10 GB retention, feeding the history-first Grafana trip index, route, summaries, diagnostics and exhaustive raw-metric table

Manufacturer-specific Mode 22 PIDs are not universally discoverable or decodable. They require the make/model/year and an appropriate definition set. The standard inventory above is deliberately exhaustive without guessing proprietary commands.

## Optional laptop serial check

Install PlatformIO and clone the focused fork:

```bash
git clone https://github.com/kierandrewett/Freematics.git
cd Freematics
pio run -e esp32dev
pio device monitor --port /dev/ttyUSB0 --baud 115200
```

Expected milestones include:

```text
DEVICE ID:ZKUCALJ0
TYPE:14
OBD:OK
CELL:SIM7670E-LN
APN:simbase
[CELL] In service
[DAT x2]
[HTTP] OK 10
```

The batch count varies. `[DAT x1]` is normal when only one sample is ready; `[DAT x16]` is the maximum backlog batch. The number after `HTTP OK` is the count of decoded values, not the count of requests.

`OBD:NO` means the ECU is not awake, the unit is not seated in the OBD socket, or the vehicle/protocol has not responded. It does not indicate a SIM or server failure.

## Rebuilding or reflashing

The committed `local_config.h.example` documents configuration, but Wi-Fi and telemetry credentials are intentionally not committed. The collector is gated by Caddy on both public and private ingress, so production firmware must be built with the device's 64-character bearer token obtained through the authorised server-secret process. To reproduce it on another unit, create an ignored `local_config.h`, then run:

```bash
PRODUCTION_BUILD=1 FREEMATICS_TOKEN="$device_token" pio run -e esp32dev -t upload --upload-port /dev/ttyUSB0
```

`FREEMATICS_TOKEN` must be exactly 64 hexadecimal characters. Never paste the token into Git, shell history, screenshots or support logs.

Do not clear diagnostic codes from the firmware or dashboard during initial testing. Reading codes is non-destructive; clearing them can erase useful freeze-frame evidence and readiness state.
