# Freematics Model B car test runbook

This is the portable hand-off for the Freematics ONE+ Model B installed as device `ZKUCALJ0`. The device is already flashed with the production firmware from source commit `b39c9f76074a09f14f2118ff4f739ceea197d7ca` and does not need a laptop to send telemetry.

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

The final USB test registered on operator `23415`, received a cellular IP, enabled strict TLS verification, authenticated, and received repeated `HTTP 200` responses from the collector. Live samples are grouped for up to five seconds and outage backlogs drain in complete, ordered batches of up to 16 samples per request.

## Light, buzzer and standby behaviour

- Online: the LED is on only while a real HTTPS telemetry request is in flight. One flash represents one request, which may contain several samples.
- Initial network search: slow repeating flash.
- Reconnecting after a previously healthy link: rapid flash.
- Parked standby: one very brief flash every 10 seconds; this is intentional modem sleep, not an outage.
- Network offline for 15 seconds while working: two low beeps once. Brief LTE handovers do not chirp repeatedly.
- Network restored after an announced outage: one short high confirmation chirp.

On the bench, the unit enters standby after approximately 180 seconds without motion. It wakes immediately above the configured `0.4 g` motion threshold and otherwise performs a cellular ping-back every 15 minutes.

## Move the unit to the car

1. Turn the ignition fully off.
2. Unplug the Freematics unit from USB.
3. Insert it firmly into the car's OBD-II socket.
4. Turn the ignition on without starting the engine.
5. Leave it powered for 90 seconds while it discovers the ECU, reads the VIN and supported PIDs, scans stored/pending/permanent fault codes, acquires GNSS, and registers on LTE.
6. Check the device at [Freematics Admin](https://freematics-admin.drewett.dev/) or open the [Grafana vehicle dashboard](https://grafana.drewett.dev/d/freematics-vehicle?var-device=ZKUCALJ0).
7. Once ECU data is visible, start the engine and leave it idling for two minutes. This adds live RPM, load, temperatures, fuel/air readings, voltage, and other ECU-advertised values to the inventory.

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

The committed `local_config.h.example` documents configuration, but Wi-Fi and telemetry credentials are intentionally not committed. The already-flashed unit contains the working configuration. To reproduce it on another unit, create an ignored `local_config.h`, obtain the device's 64-character token through the authorised server-secret process, then run:

```bash
PRODUCTION_BUILD=1 FREEMATICS_TOKEN="$device_token" pio run -e esp32dev
PRODUCTION_BUILD=1 FREEMATICS_TOKEN="$device_token" pio run -e esp32dev -t upload --upload-port /dev/ttyUSB0
```

The production build fails closed when `FREEMATICS_TOKEN` is absent or is not exactly 64 hexadecimal characters. Never paste the token into Git, shell history, screenshots or support logs.

Do not clear diagnostic codes from the firmware or dashboard during initial testing. Reading codes is non-destructive; clearing them can erase useful freeze-frame evidence and readiness state.
