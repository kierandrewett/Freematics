# Freematics Model B TeleLogger

This is Kieran Drewett's focused fork of the upstream [Freematics](https://github.com/stanleyhuangyc/Freematics) project. The repository root is a production TeleLogger firmware for Freematics ONE+ Model B, with only its required libraries and the matching collector retained.

It collects OBD-II, GNSS, accelerometer, device and network telemetry, writes an acknowledged journal to microSD in the SD build, and sends data from that durable journal to a private Freematics-compatible server over HTTPS. Cellular is preferred and Wi-Fi is the fallback.

Data Collection
---------------

The sketch collects following data.

* Vehicle OBD data (from OBD port)
* Battery voltage (from OBD port)
* Geolocation data (from internal or external GNSS) 
* Accelerometer and gyroscope data (from internal MEMS motion sensor)
* Cellular or WiFi network signal level
* Device temperature

In the SD build, each completed sample is synchronously appended to the CRC-checked SD journal and read back before the sampler can capture another one. There is no completed-sample RAM backlog or RAM upload source; one PSRAM sample buffer and one bounded serialization workspace are transient working memory only. Slow SD operations can therefore make the 250 ms sampler miss deadlines, which are counted rather than filled with repeated cached values. If the journal is unhealthy, live USB telemetry continues, but affected readings and waveform points are counted as missed and never uploaded. Acknowledgements advance a separate checksummed cursor; failed uploads or restarts replay unacknowledged SD records. Card failure, long SD latency, or power loss can still produce real gaps, so absolute zero-loss is not guaranteed. Each held OBD value includes its acquisition age. The OBD worker makes consecutive sequential requests and targets freshness within 250 ms for RPM/speed and 1,000 ms for every other supported Mode 01 PID. ECU latency, timeouts, and bridge contention can exceed those targets; per-PID ages expose the shortfall. DTC scans are deferred unless fresh RPM and speed both report zero, avoiding their blocking requests during driving and idle troubleshooting; while stationary, a DTC timeout can still delay live PID acquisition, and the DTC age reports when scans were deferred. Cellular uploads batch up to 24 readings, wait at most one second to start a partial batch, and require the collector to confirm the expected field count before acknowledgement. The uploader runs independently from collection and replays only journaled SD records while busy or offline.
  
The passive voltage and motion worker also retains each successful acquisition at
its nominal 50 Hz cadence. Each point has its own device timestamp. Raw motion
includes gravity; voltage is the uncalibrated device input. Each 250 ms frame
carries up to 16 readings from each sensor, through the same journal and upload
path. Dropped and invalid-input counters make losses visible. Recording stops
during parked upload wrap-up after pending points drain. Actual cadence still
depends on the hardware and task load.

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

Copy `local_config.h.example` to `local_config.h` and put device-specific Wi-Fi, server, and APN values there. `local_config.h` is ignored by Git so credentials are not committed. The example uses HTTPS POST against a Freematics Hub-compatible `/api` endpoint and Simbase's `[configured carrier APN]` APN. The default source build now selects microSD storage; an explicit `STORAGE_SPIFFS` override remains available for cardless builds.

HTTPS requires the configured bearer token for the Caddy-protected collector. A private/local migration build seeds it into the existing `storage` NVS namespace; subsequent tokenless OTA releases read it there. The token is never stored in the repository, and an existing NVS value is not overwritten. NVS flash encryption is not enabled, so physical flash extraction remains possible. Wi-Fi validates the server with the ISRG Root X1 trust anchor after obtaining valid network time. The Model B SIM7670 path provisions the same CA, enables CA authentication, validates time, and sends SNI for the configured hostname. BLE remains disabled in the production profile to preserve internal ESP32 heap for TLS.

The collector requires a non-empty HTTP password (`-w`) by default. An
installation behind an authenticated HTTP reverse proxy can instead use
`-x -u 0`; this mode disables built-in HTTP authentication and the UDP listener.
If UDP is enabled, it requires a server key (`-k`). Keep the collector
listener behind the authenticated Caddy service; do not expose its HTTP or
UDP ports directly.
Each sample with a clock synchronized during the current boot also stores its
capture UTC as epoch seconds plus a millisecond remainder in the SD journal and
upload frame. The collector preserves that exact device time even when GNSS
fields are absent. A UTC value restored from NVS alone is marked untrusted
until GNSS, cellular HTTP Date, or Wi-Fi SNTP synchronizes the clock; collector
receive time is never substituted for capture time.

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
A rising three-note chime (2.0, 2.6, 3.2 kHz) marks the trip start. A falling
three-note chime marks the trip end: the car turning off, or 90 seconds
stationary with the engine running. Short stops do not split the trip.
Missing speed data suppresses warnings and does not end the trip.
A successful server response rearms the alert for a later outage. The optional host-side notifier can send state changes to the separate
`freematics-device` topic on `ntfy.drewett.dev`.

### Trip lifecycle

| Phase | Recording | Modem | Leaves when |
| --- | --- | --- | --- |
| Confirming (after boot or wake) | 250 ms | Off | RPM >= 100, OBD speed >= 2 km/h or good-quality GNSS movement starts a trip. After 45 s without any, the device returns to standby and never powers the modem. |
| Trip | 250 ms | On | Car off: the ECU stops answering for 10 s, nothing moves for 15 s and the voltage is below 13.2 V (not charging). Fallback: 3 minutes without activity. |
| Wrap-up | Paused | Uploading | The SD backlog is empty, or 2 minutes pass. Any activity returns to Trip. |
| Standby | None | Off | Motion, or charging voltage after a resting reading. |

A red light keeps the ECU answering, so it never ends a trip, even with a
stop-start engine. USB bench power skips confirmation and keeps uploading the
backlog. Each sample carries `power_phase` (0x98) and `wake_reason` (0x99:
0 power on, 1 motion, 2 charging voltage).

In standby the firmware waits for the modem to send the parked marker and
power off, then shuts down the OBD link, puts the Model B's ICM-42627
accelerometer into 50 Hz low-power mode, turns the LED off, and light-sleeps
between 250 ms checks. It performs no periodic cellular/GPS tracking while
parked. Three consecutive samples above 0.08 g wake it, filtering a single
bump or vibration. Standby does not poll OBD or wake the ECU. The Model B's
passive voltage input also wakes it when the voltage rises from resting
(12.9 V or below) to charging (13.2 V or above), which is an engine start. A
battery maintainer holding the voltage up never shows a resting reading, so
it cannot cause repeated wakes. OBD is read after the device wakes.
If the SD journal is unhealthy, samples are not retained in RAM for standby or
later upload. Live USB telemetry remains available and the missed-reading and
storage-health fields expose the recording gap.

In standby a weak battery (resting below 11.8 V) turns motion wakes off; only
the rise to charging voltage, that is a running engine, wakes the device.

### Uploads and data

* The collector stores a batch sample by sample. A malformed sample goes to
  `<data>/<device>/rejected.txt` with its reason, and a device clock reset
  inside a batch starts a new trip archive; the reply counts every field, so
  the firmware never resends a batch forever. A device that still receives
  HTTP 400 halves the batch, keeps the refused record in `/QUEUE.REJ` on the
  card and continues.
* Batches are zlib-compressed on the device (`teledeflate.cpp`, dynamic
  Huffman) and posted with `?z=1`: about 5.5x smaller on full-rate data. A
  refused compressed batch is resent uncompressed. Batches adapt to the link:
  up to 40 readings, halved after a failed request.
* The recorder journals every waiting reading in one SD transaction, and
  replay reads the journal ahead in 32 KB windows.
* Each sample carries the peak acceleration since the previous sample (0x9A,
  gravity removed, with its vector in 0x9B) and the minimum and maximum supply
  voltage (0x9C, 0x9D) read at 50 Hz, so hard braking and the cranking dip
  between samples are kept. GNSS speed (RMC) updates at 5 Hz.
* The sampler runs at the highest task priority, so uploads cannot delay a
  reading. Sampling starts about 5 s after power-on; the CSV trip log opens in
  the background.
* If no reading is collected for 60 s while recording, the status task
  restarts the device (wake reason 3). The SD journal survives the restart.
* The firmware is built with `-mfix-esp32-psram-cache-issue`, which revision 1
  ESP32 chips such as this Model B need for correct PSRAM data.
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

The SD build requires a FAT32-formatted microSD card for durable recording and upload. It keeps raw local trip files as well as the upload journal. The firmware retries an initial card mount three times, tearing down SPI between retries, and retries unavailable SD storage every 30 seconds while active. Each sample is synchronously committed and verified before the sampler continues; there is no RAM retry backlog. A failed mount or append leaves live USB telemetry available, counts the missed readings, and pauses upload of new samples until the SD journal recovers. The next successful journaled sample also reports bounded per-boot diagnostic counters for unavailable capture buffers, SD-unavailable captures, failed journal commits, and sampling deadline overruns. These are diagnostic counters, not buffered readings; they can be lost if power fails before the next successful journal commit, and their categories can overlap (for example, a slow append can also cause a deadline overrun). The journal is bounded to 3.75 GiB per file by FAT32 and is reclaimed only after every record in it is acknowledged. Free card space and journal health must be monitored; the firmware does not delete unacknowledged readings to make room. A high-endurance card is preferred for continuous writing. Check `local_config.h` as well as `config.h` before building: a local `STORAGE_SPIFFS` override disables the journal.

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

`sensor_waveforms` returns original voltage and motion acquisitions around an
event, with per-point timing and collection-quality evidence. Use its
`start_sequence`, `limit` and `next_sequence` fields to inspect the lead-in and
recovery. Waveforms are read from the ordered archive; the legacy live API and
Prometheus gauges do not contain those high-rate vectors. Trip summaries include
voltage range, motion candidates, cadence and losses.

`compare_baseline` also compares cold idle, warm idle, cold driving and warm
driving with up to 30 earlier sealed trips from the same device. Driving
comparisons match RPM, load and speed bins. A comparison needs five reference
trips and adequate independent acquisitions. Missing ages, stale values and
insufficient coverage stay explicit. Temperature and idle-RPM boundaries are
analysis rules, not manufacturer specifications. A difference from earlier
trips is a reason to investigate; it does not identify a failed part.

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

* `OBD_FAST_INTERVAL_MS=250UL`, the RPM and speed freshness target
* `OBD_PID_INTERVAL_MS=1000UL`, the target for every other supported Mode 01 PID
* `OBD_FAILED_RETRY_MS=1000UL`, the minimum retry interval after a failed PID
* `STANDBY_POLL_INTERVAL_MS=250UL`

Copy `.env.example` to the ignored `.env` file once. Set `PRODUCTION_BUILD=1`
and put the collector's existing 64-character token in `FREEMATICS_TOKEN`.
Use `chmod 600 .env`. PlatformIO reads this file automatically for builds
and uploads. Explicit process environment values take priority. The loader
reads values without executing shell commands.

With the intended server and APN in `local_config.h`, build and flash:

The dashboard, PlatformIO serial monitor, and `tools/measure_usb_stream.py`
must not read the same serial port at the same time. Close the dashboard before
using the monitor or measurement tool; close those tools before reconnecting
the dashboard. Competing readers split the byte stream and can look like
corrupt telemetry.

```sh
pio run -e esp32dev
sha256sum .pio/build/esp32dev/firmware.bin
pio run -e esp32dev -t upload --upload-port /dev/ttyUSB0
pio device monitor --port /dev/ttyUSB0 --baud 460800
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
