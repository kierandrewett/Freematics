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

Collected data are stored in a circular buffer in ESP32's IRAM or PSRAM. When PSRAM is enabled, hours of data can be buffered in case of temporary network outage and transmitted when network connection resumes.
  
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

WiFi HTTPS selects TLS automatically on port 443. It currently follows the SIMCOM HTTPS behaviour and encrypts traffic without verifying the server certificate. Disable BLE when using HTTPS on Model B to leave enough internal ESP32 heap for the TLS handshake.

Data Storage
------------

Following types of data storage are supported.

* MicroSD card storage
* ESP32 built-in Flash memory storage (SPIFFS)

SPIFFS does not require a microSD card. A card is only needed when `STORAGE_SD` is selected; the production configuration uses `STORAGE_SPIFFS`.

BLE & App
---------

A BLE SPP server is implemented in [FreematicsPlus](https://github.com/stanleyhuangyc/Freematics/blob/master/libraries/FreematicsPlus) library. To enable BLE support, change ENABLE_BLE to 1 [config.h](config.h). This will enable remote control and data monitoring via [Freematics Controller App](https://freematics.com/software/freematics-controller/).

Build and flash
---------------

Install PlatformIO, connect the ONE+ Model B over USB, then run:

```sh
pio run -e esp32dev
pio run -e esp32dev -t upload --upload-port /dev/ttyUSB0
pio device monitor --port /dev/ttyUSB0 --baud 115200
```

Repository layout
-----------------

* Root: Model B TeleLogger firmware and PlatformIO configuration
* `lib/`: only the FreematicsPlus, FreematicsOLED and embedded HTTP libraries required by the firmware
* `collector/`: matching Freematics Hub-compatible ingestion server

Prerequisites
-------------

* Freematics ONE+ [Model B](https://freematics.com/products/freematics-one-plus-model-b/)
* A micro SIM card if cellular network connectivity required
* [PlatformIO](http://platformio.org/), [Arduino IDE](https://github.com/espressif/arduino-esp32#installation-instructions), [Freematics Builder](https://freematics.com/software/arduino-builder) or [ESP-IDF](https://github.com/espressif/esp-idf) for compiling and uploading code
