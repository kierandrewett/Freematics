# Freematics Model B TeleLogger

This is Kieran Drewett's focused fork of the upstream [Freematics](https://github.com/stanleyhuangyc/Freematics) project. The repository root is a production TeleLogger firmware for Freematics ONE+ Model B, with only its required libraries and the matching collector retained.

It collects OBD-II, GNSS, accelerometer, device and network telemetry, buffers outages in PSRAM, writes an acknowledged journal to microSD in the SD build, and sends data to a private Freematics-compatible server over HTTPS. Cellular is preferred and Wi-Fi is the fallback.

Data Collection
---------------

The sketch collects following data.

* Vehicle OBD data (from OBD port)
* Battery voltage (from OBD port)
* Geolocation data (from internal or external GNSS) 
* Accelerometer and gyroscope data (from internal MEMS motion sensor)
* Cellular or WiFi network signal level
* Device temperature

Collected readings first occupy up to 1,024 PSRAM queue slots. The SD build then writes each complete reading to a CRC-checked append-only journal before releasing its RAM slot. Acknowledgements advance a separate checksummed cursor; a failed upload or restart replays unacknowledged readings. A full or failed card leaves readings in finite RAM and raises a health fault. If both stores fill, collection cycles are counted as missed and logged as critical rather than overwriting older captured readings. Finite storage, card failure and loss of power still prevent an absolute zero-loss guarantee. While powered, the sampler records a row every 250 ms from background sensor snapshots. Each held value includes its acquisition age. The OBD worker targets a 250 ms cycle for RPM, speed and rotating core PIDs, with one auxiliary PID interleaved per cycle. This continuously reports every ECU-advertised Mode 01 PID over a rotating scan; the ECU's serial diagnostic link and response latency bound the actual per-PID rate. Cellular uploads batch up to 24 readings, wait at most one second to start a partial batch, and require the collector to confirm the expected field count before acknowledgement. The uploader runs independently from collection, and the SD journal retains samples while it is busy or offline.
  
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

Copy `local_config.h.example` to `local_config.h` and put device-specific Wi-Fi, server, and APN values there. `local_config.h` is ignored by Git so credentials are not committed. The example uses HTTPS POST against a Freematics Hub-compatible `/api` endpoint and Simbase's `simbase` APN. The default source build now selects microSD storage; an explicit `STORAGE_SPIFFS` override remains available for cardless builds.

HTTPS requires the configured bearer token for the Caddy-protected collector. The token is injected at build time and is never stored in the repository. Wi-Fi validates the server with the ISRG Root X1 trust anchor after obtaining valid network time. The Model B SIM7670 path provisions the same CA, enables CA authentication, validates time, and sends SNI for the configured hostname. BLE remains disabled in the production profile to preserve internal ESP32 heap for TLS.

The collector requires a non-empty HTTP password (`-w`) by default. An
installation behind an authenticated HTTP reverse proxy can instead use
`-x -u 0`; this mode disables built-in HTTP authentication and the UDP listener.
If UDP is enabled, it requires a server key (`-k`). Keep the collector
listener behind the authenticated Caddy service; do not expose its HTTP or
UDP ports directly.
The collector acknowledges a telemetry POST only after the raw archive batch
has been flushed and synced to disk. If the archive cannot be opened, written,
or synced, it returns HTTP 503 so the device retains and retries the batch.
The collector validates every timestamp in a multi-sample POST before writing it.
Retrying a batch that ends at the last accepted timestamp keeps the same trip archive.
Replay can still duplicate a batch after a lost response, and older queued
readings can inherit a later collector trip. The reading-identity and durable
inbox migration is specified in
[`monitoring/INGEST_RELIABILITY.md`](monitoring/INGEST_RELIABILITY.md).
The web portal returns device IDs only. Manage Traccar credentials through its
separate authenticated service.
Device notification state changes use POST. Legacy GET notifications require
the same 64-character bearer format and are not accepted from browsers.

The onboard LED shows network state and pulses only during an active telemetry
upload when the network is online. Routine network changes stay silent, but
the device sounds three short beeps once it has gone 60 seconds without an
accepted Freematics server response during a driving trip. Warning beeps sound
only while fresh speed shows movement at 3 km/h or more. Engine idle and
vibration do not count. Fresh OBD speed takes priority; a fresh GPS fix with
at least four satellites and HDOP at most five supplies the fallback.
A rising two-note chime marks the trip start. A falling two-note chime marks
90 seconds stationary, or entry into standby. Short stops do not split the
trip. Missing speed data suppresses warnings and does not end the trip.
A successful server response rearms the alert for a later outage. The optional host-side notifier can send state changes to the separate
`freematics-device` topic on `ntfy.drewett.dev`.
Recording runs every 250 ms while the engine runs or the car moves. After
three minutes with no fresh RPM of 100 or more, no OBD speed of 2 km/h or more
and no good-quality GNSS movement, the new fork firmware enters standby. It
waits for the modem to send the parked marker and power off, then shuts down
the OBD link, puts the Model B's ICM-42627 accelerometer into
50 Hz low-power mode, turns the LED off, and light-sleeps between 250 ms motion
checks. It sends one parked marker on entry, then performs no periodic
cellular/GPS tracking while parked. Three consecutive samples above 0.08 g are
required to wake the active collection path, filtering a single bump or
vibration. Motion returns the unit to the active collection path automatically.
Standby does not poll OBD or wake the ECU. OBD speed is read after the motion
sensor wakes the device. Only a failed or absent motion sensor permits the
Model B's passive voltage input to wake it at charging voltage.
If readings exist only in RAM, it stays active instead of rebooting into
standby and losing them. This can increase parked power draw until storage or
the server recovers.
The USB-connected Model B was flashed on 27 September 2026 with the normal
`sd-retry-20260927` build (SHA-256
`f1c697ef310b8b4a5e6d8fd7baf32c6791e4a2e5a34c3f14b06349c229c30fd0`).
The preceding SD journal image's serial log showed a 29,992 MB card, journal replay across reboot,
collector `OK` acknowledgements, and continued journal writes after the last
backlog record was acknowledged. The indexed health fields showed durable
backlog `0`, journal healthy `1`, and missed readings `0` at the end of the
bench run. Confirm the build and health fields again after any future flash.
This build can seed the ESP32 clock from a valid GNSS position/time if the
cellular network supplies neither NITZ nor NTP time, then seed the modem on a
later HTTPS attempt. Certificate verification remains enabled. The USB bench
had no GNSS fix, so cellular recovery through this path still needs a drive
outside Wi-Fi coverage.

Data Storage
------------

Following types of data storage are supported.

* MicroSD card storage
* ESP32 built-in Flash memory storage (SPIFFS)

The SD build requires a FAT32-formatted microSD card for durable queueing. It keeps raw local trip files as well as the upload journal. The installed image retries an initial card mount three times and retries unavailable SD storage every 30 seconds while active; a failed mount leaves readings in the finite RAM queue and reports unhealthy storage. A sample becomes available to the uploader only after the journal write finishes, or when the card write fails and RAM retains it. The journal is bounded to 3.75 GiB per file by FAT32 and is reclaimed only after every record in it is acknowledged. This bound, the card's free space and the finite RAM queue must be monitored; the firmware does not delete unacknowledged readings to make room. A high-endurance card is preferred for continuous writing. Check `local_config.h` as well as `config.h` before building: a local `STORAGE_SPIFFS` override disables the journal.

A card reader is not required for first-time formatting. The normal image never formats a card on mount failure. If a newly inserted card is unformatted or uses an unsupported filesystem, a one-time provisioning build with both `FREEMATICS_FORMAT_SD_ONCE=1` and `FREEMATICS_FORMAT_SD_CONFIRM=ERASE_UNFORMATTED_CARD` allows the Arduino SD driver to format only when it reports no FAT filesystem. This erases that card. Confirm the card has no data to keep, flash the provisioning image, check the serial `SD:` capacity and `[QUEUE] SD journal ready` lines, then flash the normal image without those environment variables. Nothing typed into the serial monitor can start formatting. If the card already mounts, the provisioning image does not format it. A failed mount for another reason still needs diagnosis.

Actual accepted frame excerpt (27 September 2026; first two readings of a 24-reading POST):

```text
0:15070,85:0,86:0,87:0,88:0,89:0,8A:0,84:2,24:376,20:0;0;0,30:0,82:35,8B:0,8C:0,
0:15082,85:0,86:0,87:0,88:0,89:0,8A:0,84:2,24:367,20:0;0;0,30:0,82:35,8B:1,8C:94,...*4B
```

The real POST was 2,004 bytes and ended in checksum `*4B`. `0` is device monotonic time in milliseconds, `24` is encoded battery voltage (`376` means 3.76 V), `84:2` is the transport recorded when that reading was captured, and `8B`/`8C` describe the queued readings and bytes. The accepted upload itself used Wi-Fi. Values without a valid GPS fix omit location.

The host `collector/trip_intelligence.py` runs a fast event worker and a separate trip-analysis worker against the SQLite history. Events notify `freematics-device` about fresh movement, stops, valid locations, storage failures, missed collection cycles, and newly observed stored, pending or permanent fault codes. Each completed trip is screened by Jev through OpenRouter using the full observed OBD PID inventory, a time-windowed timeline for every continuous PID, matched-operation comparisons and this vehicle's prior-trip behaviour. Jev decides whether there is a plausible mechanical concern, a useful driving pattern or a meaningful cross-trip change. Confirmed fault codes go straight to the mechanic review. Routine trips stop after the low-cost screen; only positive classifications use the ChatGPT subscription for the full GPT-6 review. The screen decision and returned probabilities are stored for audit. The OpenRouter key is used only for Jev screening; the deployed full report provider remains Codex.

The analysis worker runs the Codex CLI with a ChatGPT subscription and `gpt-6-sol`; the CLI investigates via the authenticated, read-only MCP server at `https://freematics.drewett.dev/mcp`. Its tools expose the full PID catalogue, per-PID time series, aligned sample windows, and source-order raw fields including repeated PIDs. It measures consecutive OBD speed changes where sampling is close enough and compares episode rates with earlier driving trips when enough are available. These summaries guide investigation; the model must inspect underlying samples before making a finding. Stored earlier reports provide hypotheses for later reviews but are not treated as evidence without checking the data. The investigation may be detailed, but the owner report is capped at one short conclusion, three brief measured observations, two supported issues, and two material limitations. Reports exceeding that format are rejected and retried. It stores each report and its model receipt before notification, retries failed calls with backoff, and alerts when analysis is delayed. Older indexed trips with a prior report are re-screened one at a time without retrospective push notifications.

The MCP tools expose the vehicle's observed standard OBD PIDs with named units, aligned raw samples, timestamped signal series, chronological trip summaries, prior driving-trip comparisons, fault-code history, data quality and stored reports. Codex can ask follow-up questions of the data and use live web search for relevant standards or manufacturer information. These comparisons identify candidates for investigation, not a failed part by themselves. A MAF change can also reflect temperature, EGR, boost, sensor or route differences. Generic Mode 01 data does not contain per-cylinder misfire counts, and vehicle-specific tolerances require a verified engine variant and applicable documentation. Bench sessions and tiny fragments do not produce trip reports. Missing GPS fixes, data gaps, absent signals and uncertain wall-clock timestamps are excluded from vehicle fault analysis. Provider credentials and the MCP bearer token stay on the host; neither belongs in the firmware.

Jev is the low-cost trip screen, not the mechanic diagnosing faults or calculating measurements. It returns typed judgments with probabilities; application code decides whether to request a full report. Screening decisions are retained so thresholds and missed cases can be reviewed as the labeled trip archive grows. Numeric measurements and comparisons stay in code, and Codex reads the underlying samples for any escalated trip. The Jev screen requires `OPENROUTER_API_KEY`; without it, non-DTC screening retries rather than silently treating a trip as uninteresting.

The MCP endpoint accepts a separate, random read-only bearer token, distinct from the firmware's upload token. The service enforces it before handling a request; Caddy also gates `/mcp`. On this workstation the token is in `~/.config/freematics/mcp-token` (mode 600). The installed Codex `freematics` MCP connection uses `~/.local/bin/freematics-mcp-headers` to supply the header from that file. To connect another Codex client, put the token in a private environment variable and run `codex mcp add freematics --url https://freematics.drewett.dev/mcp --bearer-token-env-var FREEMATICS_MCP_TOKEN`, then set `mcp_servers.freematics.default_tools_approval_mode = "writes"` for these read-only tools. The server-side worker and management page share a persistent `CODEX_HOME=/state/codex`. [Freematics](https://freematics.drewett.dev/) is a trip archive and concise mechanic review: select a journey for measured observations and fault codes, then **Explore data in Grafana** to open its route and time series over that trip's recorded interval. Earlier verbose reports remain stored for provenance but are hidden while the worker replaces them, one trip per pass. If ChatGPT is not connected, the page offers **Sign in to ChatGPT** and shows the device-code flow inside the Pocket ID protected page. Until sign-in succeeds, reports remain queued and retry; fast event alerts continue independently. The live collector at [Freematics Admin](https://freematics-admin.drewett.dev/) is also behind Pocket ID. The firmware upload and bearer-authenticated MCP routes remain independent of browser sign-in.

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

Install PlatformIO, create an ignored `local_config.h` from the example, and
connect the ONE+ Model B over USB. Production builds require the 64-character
bearer token used by the Caddy-protected collector. Keep these committed
sampling settings unchanged unless the vehicle test plan records a new
cadence:

* `OBD_FAST_INTERVAL_MS=250UL`, with RPM plus two rotating core PIDs per cycle
* `OBD_AUX_INTERVAL_MS=250UL`, with one interleaved auxiliary PID per cycle
* `STANDBY_POLL_INTERVAL_MS=250UL`

Copy `.env.example` to the ignored `.env` file once. Set `PRODUCTION_BUILD=1`
and put the collector's existing 64-character token in `FREEMATICS_TOKEN`.
Use `chmod 600 .env`. PlatformIO reads this file automatically for builds
and uploads. Explicit process environment values take priority. The loader
reads values without executing shell commands.

With the intended server and APN in `local_config.h`, build and flash:

```sh
pio run -e esp32dev
sha256sum .pio/build/esp32dev/firmware.bin
pio run -e esp32dev -t upload --upload-port /dev/ttyUSB0
pio device monitor --port /dev/ttyUSB0 --baud 115200
```

Production builds reject missing or malformed tokens and invalid deployment
settings before creating an image. Keep the real `.env` local; it must remain
outside Git. Card formatting options are rejected in `.env` because they must
be explicit one-time process options. Record the image SHA-256 and the boot
build ID and device ID after each flash.

Repository layout
-----------------

* Root: Model B TeleLogger firmware and PlatformIO configuration
* `lib/`: only the FreematicsPlus, FreematicsOLED and embedded HTTP libraries required by the firmware
* `collector/`: matching Freematics Hub-compatible ingestion server, rebuildable SQLite history indexer, safe telemetry mirror, and identity-gated vehicle profile registry
* `collector/ui-src/`: the shadcn/React mechanic report page. Run `npm ci` and `npm run build` there, then copy `dist/` into `collector/ui/` before deploying the UI service. Grafana remains the trip charting and map interface; the report page links each trip to Grafana with its stored time range.
* `protocols/`: standards and manufacturer profile evidence. Opel/Vauxhall Corsa D candidates remain read-only and disabled until VIN, engine code, ECU address, and raw positive responses are captured.
* `monitoring/`: generated Grafana dashboards and their maintainable Python source. The combined dashboard shows the complete SQLite trip archive above its live charts. Click a **Trip** value to open the historical dashboard with Grafana's time range set to that trip's stored start and end, plus 30 seconds on each side. The historical dashboard contains the route, quality evidence, diagnostics, efficiency, performance, and raw metrics. Selecting a trip in the **Trip** dropdown also sets the time range to that trip's window. A slim panel at the top of the trips view does this with the [Business Text](https://grafana.com/grafana/plugins/marcusolsson-dynamictext-panel/) plugin (`marcusolsson-dynamictext-panel` 6.3.0), which must be installed in Grafana. Zoom and pan are kept until another trip is selected. `monitoring/e2e_trip_time_range.py` checks this in a real browser against a running Grafana and writes a JSON artifact. Empty sessions with zero samples have no time range to graph. Provision `grafana-live.json` for fresh telemetry and `grafana-trips.json` for the historical view; `grafana-dashboard.json` is the combined dashboard. The durable SQLite projection is built with `PYTHONPATH=collector python3 collector/history_indexer.py --archive-root /path/to/data --database /path/to/history.sqlite --once`; see [`monitoring/HISTORICAL_SQL.md`](monitoring/HISTORICAL_SQL.md).

Prerequisites
-------------

* Freematics ONE+ [Model B](https://freematics.com/products/freematics-one-plus-model-b/)
* A micro SIM card if cellular network connectivity required
* [PlatformIO](http://platformio.org/), [Arduino IDE](https://github.com/espressif/arduino-esp32#installation-instructions), [Freematics Builder](https://freematics.com/software/arduino-builder) or [ESP-IDF](https://github.com/espressif/esp-idf) for compiling and uploading code
