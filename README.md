# Freematics Model B TeleLogger

This is Kieran Drewett's focused fork of the upstream [Freematics](https://github.com/stanleyhuangyc/Freematics) project. The repository root is a production TeleLogger firmware for Freematics ONE+ Model B, with only its required libraries and the matching collector retained.

It collects OBD-II, GNSS, accelerometer, device and network telemetry, buffers outages in PSRAM, logs to internal SPIFFS, and sends data to a private Freematics-compatible server over HTTPS. Cellular is preferred and Wi-Fi is the fallback.

Data Collection
---------------

The sketch collects following data.

* Vehicle OBD data (from OBD port)
* Battery voltage (from OBD port)
* Geolocation data (from internal or external GNSS) 
* Accelerometer and gyroscope data (from internal MEMS motion sensor)
* Cellular or WiFi network signal level
* Device temperature

Collected data are stored in a circular buffer in ESP32's IRAM or PSRAM. The production Model B build has 1,024 queue slots: this is intentionally finite outage insurance (about 8.5 minutes at the 500 ms moving cadence), not a long-term archive. Internal SPIFFS keeps bounded rotating log chunks, and a microSD card is recommended when an outage may last longer than the in-memory queue or when local retention matters. Moving vehicles sample speed, RPM, load and throttle at 500 ms; slower PIDs are rotated every five seconds so the OBD bridge stays responsive. Samples are posted in five-second batches, with up to 24 complete samples per request while draining a backlog; a failed request retains the whole batch for ordered retry. The wire format remains the existing compact PID:value frame with one checksum per batch, avoiding JSON and repeated HTTPS headers.
  
Data Transmission
-----------------

Data transmission over UDP and HTTP(s) protocols are implemented for the followings.

* WiFi (ESP32 built-in)
* 3G WCDMA (SIM5360)
* 4G LTE CAT-4 (SIM7600)
* 4G LTE CAT-1 (SIM7670)
* 4G LTE CAT-M (SIM7070)

UDP mode implements a telemetry client for [Freematics Hub](https://hub.freematics.com) and [Traccar](https://www.traccar.org). HTTP(s) mode implements [OsmAnd](https://www.traccar.org/osmand/) protocol with additional data sent as POST payload.

The production configuration prefers the Model B SIM7670 cellular modem. If cellular cannot connect, it falls back to the configured Wi-Fi network and periodically retries cellular.

Local configuration
-------------------

Copy `local_config.h.example` to `local_config.h` and put device-specific Wi-Fi, server, and APN values there. `local_config.h` is ignored by Git so credentials are not committed. The example uses HTTPS POST against a Freematics Hub-compatible `/api` endpoint, internal SPIFFS storage, and Simbase's `simbase` APN.

HTTPS requires the configured bearer token for the Caddy-protected collector. The token is injected at build time and is never stored in the repository. Wi-Fi validates the server with the ISRG Root X1 trust anchor after obtaining valid network time. The Model B SIM7670 path provisions the same CA, enables CA authentication, validates time, and sends SNI for the configured hostname. BLE remains disabled in the production profile to preserve internal ESP32 heap for TLS.

The onboard LED shows network state and pulses only during an active telemetry
upload when the network is online. The production profile keeps the buzzer
disabled for routine network changes; the optional host-side notifier can send
state changes to the separate `freematics-device` topic on `ntfy.drewett.dev`.
After approximately three minutes without motion, the new fork firmware shuts
down the radios and OBD link, puts the Model B's ICM-42627 accelerometer into
50 Hz low-power mode, turns the LED off, and light-sleeps between 250 ms motion
checks. It sends one parked marker on entry, then performs no periodic
cellular/GPS tracking while parked. Three consecutive samples above 0.5 g are
required to wake the active collection path, filtering a single bump or
vibration. Motion returns the unit to the active collection path automatically.
The production device was flashed from fork commit `12dc207bc559` and
confirmed over serial with bearer authentication, modem time, strict TLS, and
accepted HTTPS posts.

Data Storage
------------

Following types of data storage are supported.

* MicroSD card storage
* ESP32 built-in Flash memory storage (SPIFFS)

SPIFFS does not require a microSD card for normal connected operation. A card is only needed when `STORAGE_SD` is selected; the production configuration uses `STORAGE_SPIFFS`, rotates log chunks at 256 KB and purges the oldest chunks before internal flash fills. Neither SPIFFS nor the RAM queue is a six-month offline archive; use a high-endurance microSD card for that requirement.

Unattended vehicle power
------------------------

The OBD socket is normally connected to the vehicle battery. Freematics publishes
approximately 10 mA as the Model B low-power floor with the radios and GPS off;
the actual installed current must be measured because the firmware still monitors
the motion sensor while parked. That floor alone is about 44 Ah over six months,
before the car's own parasitic load. Do not leave the unit connected for months
without a measured current budget, a switched/low-voltage-cutoff OBD supply, or a
vehicle-specific battery plan. This repository does not claim six-month battery
operation without that hardware validation.

BLE & App
---------

A BLE SPP server is implemented in [FreematicsPlus](https://github.com/stanleyhuangyc/Freematics/blob/master/libraries/FreematicsPlus) library. To enable BLE support, change ENABLE_BLE to 1 [config.h](config.h). This will enable remote control and data monitoring via [Freematics Controller App](https://freematics.com/software/freematics-controller/).

Build and flash
---------------

Install PlatformIO, create an ignored `local_config.h` from the example, and connect the ONE+ Model B over USB. Production builds require the 64-character bearer token used by the Caddy-protected collector:

```sh
PRODUCTION_BUILD=1 FREEMATICS_TOKEN="$device_token" pio run -e esp32dev -t upload --upload-port /dev/ttyUSB0
pio device monitor --port /dev/ttyUSB0 --baud 115200
```

`FREEMATICS_TOKEN` must be exactly 64 hexadecimal characters. Do not store it in the repository or a shared shell script.
The boot log prints `[BOOT] Build:` with the short Git revision (or `-dirty` when the source tree was modified), which should be recorded after every production flash.

Repository layout
-----------------

* Root: Model B TeleLogger firmware and PlatformIO configuration
* `lib/`: only the FreematicsPlus, FreematicsOLED and embedded HTTP libraries required by the firmware
* `collector/`: matching Freematics Hub-compatible ingestion server
* `monitoring/`: generated Grafana dashboards and their maintainable Python source. Provision `grafana-live.json` for the current device link and fresh telemetry, and `grafana-trips.json` for the historical trip index, route and selected-trip evidence. `grafana-dashboard.json` remains as a backwards-compatible combined dashboard.

Prerequisites
-------------

* Freematics ONE+ [Model B](https://freematics.com/products/freematics-one-plus-model-b/)
* A micro SIM card if cellular network connectivity required
* [PlatformIO](http://platformio.org/), [Arduino IDE](https://github.com/espressif/arduino-esp32#installation-instructions), [Freematics Builder](https://freematics.com/software/arduino-builder) or [ESP-IDF](https://github.com/espressif/esp-idf) for compiling and uploading code
