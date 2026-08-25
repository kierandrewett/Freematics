# Freematics Model B car test runbook

This is the portable hand-off for the Freematics ONE+ Model B installed as device `ZKUCALJ0`. The device is already flashed from source commit `984db62df5b5f7940a9e313e18ec8537a6f4552e` and does not need a laptop to send telemetry.

## Known-good configuration

- Hardware: Freematics ONE+ Model B (`TYPE:14`)
- Modem: SIMCom SIM7670E-LN
- SIM provider: Simbase UK
- APN: `simbase`
- Network order: cellular first, Wi-Fi fallback
- Transport: HTTPS to `freematics.drewett.dev:443`
- Device ID: `ZKUCALJ0`
- Storage: internal SPIFFS; no SD card is required
- SIM orientation: insert it as shown in the Freematics Model B product image, with the electrical contacts facing up

The final USB test registered on operator `23415`, received a cellular IP, logged in over HTTPS, posted telemetry, and received `HTTP 200` from the collector.

## Move the unit to the car

1. Turn the ignition fully off.
2. Unplug the Freematics unit from USB.
3. Insert it firmly into the car's OBD-II socket.
4. Turn the ignition on without starting the engine.
5. Leave it powered for 90 seconds while it discovers the ECU, reads the VIN and supported PIDs, scans stored/pending/permanent fault codes, acquires GNSS, and registers on LTE.
6. Check the device at [Freematics Admin](https://freematics-admin.drewett.dev/) or open the [Grafana vehicle dashboard](https://grafana.drewett.dev/d/freematics-vehicle/vehicle-c2b7-freematics?var-device=ZKUCALJ0).
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
[HTTP] OK
```

`OBD:NO` means the ECU is not awake, the unit is not seated in the OBD socket, or the vehicle/protocol has not responded. It does not indicate a SIM or server failure.

## Rebuilding or reflashing

The committed `local_config.h.example` documents configuration, but credentials are intentionally not committed. The already-flashed unit contains the working configuration. To reproduce it on another unit, create an ignored `local_config.h`, then run:

```bash
pio run -e esp32dev
pio run -e esp32dev -t upload --upload-port /dev/ttyUSB0
```

Do not clear diagnostic codes from the firmware or dashboard during initial testing. Reading codes is non-destructive; clearing them can erase useful freeze-frame evidence and readiness state.
