# Device recovery acceptance

## Failure cases recorded before implementation

- Boot or component setup blocks before the independent alarm starts.
- Login succeeds but no telemetry batch is accepted. The alarm must still fire.
- Collection stops, the SD card fails, or the RAM queue fills while login remains healthy.
- Standby ignores ignition, normal vehicle motion, a failed motion sensor, or a missing motion sensor.
- An OBD retry delays all recording on a USB-powered device with no ECU.
- Cellular has an IP address but no trustworthy clock. Certificate checks must remain enabled.
- The modem restarts on a valid clock write. Host TLS must work without that write.
- TLS rejects an invalid certificate or hostname before sending credentials.
- The installed Arduino SDK omits automatic certificate date checks; verify all certificate dates explicitly.
- Binary TLS bytes, partial socket writes, HTTP chunking and truncated responses must not produce a false acknowledgement.
- An SD append fails, a record is partial, or a cursor is invalid. Keep original journal bytes, pause upload, and
  count/drop any sample without a verified SD copy rather than making RAM the outage archive.
- A failed existence check is followed by a truncating journal create. Use exclusive creation so existing bytes cannot be erased.
- Repeated I/O attempts on a failed card stall collection and USB diagnostics. Retry at a bounded rate and keep
  live USB diagnostics responsive.
- Fresh RAM readings prevent an older SD backlog from ever being replayed. Upload only from the SD journal and
  preserve its acknowledgement order after recovery.
- USB bench power enters parked standby before the existing SD backlog has uploaded.
- Uploaded data disappears from the local archive.
- An unbounded archive retention scan stalls fresh collection for seconds as the directory grows.
- Retention removes recent data, data of unknown age, the active file, or unacknowledged journal data.
- An SD failure produces only one warning and then remains silent. Repeat three
  beeps every five seconds until fresh local recording recovers.
- Accepted cellular data, disabled server alerts or a different upload protocol
  suppress the SD alarm. Local recording failure must have its own alarm.
- Standby silences an existing recording failure. Keep an existing fault alarm
  active; deliberate healthy standby alone does not create a failure.
- Startup blocks before storage is checked, or collection stops after setup.
  Warn after 15 seconds without progress, and continue the alarm.
- An SD remount reports healthy before a fresh sample is recorded. Do not clear
  the alarm until storage is healthy and a new sample has completed.
- Removing USB SD export must preserve local journal writes, 14-day retention,
  cellular transport and the independent repeating alarm.

## Current evidence

Evidence directory: `/tmp/freematics-review-20260929` (private permissions).
Repeating-alarm evidence: `/home/kieran/.cache/freematics-review-20260929`.

- The connected device is `ZKUCALJ0`, hardware type 14, ESP32-D0WDQ6,
  with a CH340 USB bridge (`1a86:7523`) and SIM7670E-LN modem.
- Valid `AT+CCLK` writes repeatedly restarted modem firmware
  `2374B03SIM767XM5A`. Network time services did not initialise its clock.
  Cellular TCP connections to the collector on ports 80 and 443 succeeded.
- HTTPS now runs on the ESP32 over the cellular TCP socket. It requires
  TLS 1.2, the root CA and matching hostname. Explicit certificate date
  checks are necessary because this Arduino SDK omits automatic date checks.
  The hardware rejected the wrong-hostname connection and an invalid date,
  then recovered to a valid HTTPS connection.
- The normal logger received 47 collector acknowledgements for 23,721
  values in one bench run. A response timeout caused reconnect and replay.
  The server created and grew a new telemetry file for this device.
- That run recorded fresh CSV samples and reduced the SD backlog. Later
  reboots reproduced SD I/O failures within about 30 seconds. SD reliability
  is **not established**. Disabling retention and lowering the SD clock did
  not prevent the fault. A later minimal probe failed to mount the card before
  any journal create or write, with the modem radio disabled. The original
  journal could not be scanned in that probe. Do not claim that USB power is
  the confirmed cause.
- The three-beep warning was heard by Kieran. The independent alarm also
  fired in the live run when the SD journal failed.
- A physical scratch-journal check acknowledged three records and retained
  matching archive bytes. It did not inject test samples into the real
  journal. The original journal remained 3,681,396 bytes in that check.
- Every deployed candidate in this review used `ENABLE_WIFI=0`.
- The earlier production image booted as `cell-sd-guard-20260929`. USB status
  confirmed Wi-Fi disabled and the 14-day retention setting. Cellular HTTPS
  accepted fresh readings while the failed card remained unavailable.

## Firmware faults corrected

The previous wake path depended on a high motion threshold and did not reliably
recover from an absent or failed motion sensor. Ignition voltage and an ECU
probe now provide additional wake paths. The alarm starts before component
setup, and successful login cannot silence an alarm for missing accepted data.

The buzzer code replaced the tone's 50 percent duty cycle with a nearly constant
output. The tone now retains its normal duty cycle. Kieran heard the warning
on the connected hardware.

The original SD warning sounded once and depended on server-alert settings.
The SD alarm now has its own check and repeats three beeps every five seconds.
A known mount or write failure starts the alarm as soon as the completed
storage check or write reports failure. A blocked startup or stopped collector
starts the alarm after 15 seconds without progress. Cellular acknowledgements
cannot stop it. An existing fault continues to warn during standby. Recovery
requires healthy storage and a fresh completed sample. Healthy deliberate
standby does not create a new fault.

The modem clock write repeatedly restarted the modem. HTTPS now uses the ESP32
TLS implementation over cellular TCP. It verifies certificates, hostname and
dates before it sends the telemetry credential.

The journal create path could truncate an existing file after a failed existence
check. Exclusive creation prevents that path. ESP-IDF maps `O_CREAT | O_EXCL`
to FAT `FA_CREATE_NEW`; see the [driver source](https://github.com/espressif/esp-idf/blob/v4.4.7/components/fatfs/vfs/vfs_fat.c).
The on-device fault-injection check of this final create path remains incomplete
because the card failed to mount first. The earlier physical archive check passed.

The dashboard displayed "Starting" for a device with no recent telemetry.
It now displays "No recent telemetry". That label was verified in live Grafana.

## USB SD feature removed

Kieran withdrew the USB SD request on 29 September. The firmware SD export
module, FSD command parser, USB clock/beep commands, diagnostic host reader
and export command test are removed. The final image contains none of the
export command or response strings. No filesystem mount is installed.

USB still supplies power and supports firmware flashing and ordinary serial
logs. Local SD recording, cellular upload, 14-day retention and the repeated
recording fault warning remain enabled.

## Recovery and retention behaviour

A complete sample must reach a verified SD journal write before its short
sampler-to-recorder handoff slot is released. A failed POST retains the batch
on SD for retry. Only an exact collector acknowledgement advances the saved
cursor. A login or ping cannot silence the missing-data alarm. SD recovery
never formats the card.

The current SD source does not retain failed or unjournaled samples in RAM for
later upload. While the journal is unhealthy, USB live telemetry continues,
but local samples are counted as missed, pending waveform points are dropped
and reported, and uploads pause. This makes the gap explicit rather than
pretending the SD record exists. The requested continuous history therefore
still depends on resolving the intermittent physical card/drive readiness
fault; there is no second durable medium to cover an SD outage.

Once all journal records are acknowledged, the journal is renamed into
`/DATA/<id>.BIN` and retained locally. CSV and binary archives expire after
14 days. Files with unknown creation time receive a full new 14-day window.
Unacknowledged data is never removed by retention. A failed existence check
cannot truncate an existing journal because creation uses `O_EXCL`.

On USB power, an existing backlog keeps the upload task active beyond the
parked idle cutoff. Parked motion checks use the normal light-sleep policy.

## Acceptance gates

- [x] Twenty lifecycle/protocol checks passed, including ignition, normal motion,
  failed/absent motion sensors, login-only alarm, repeat alarms, recovery and
  healthy quiet standby. SD warnings also passed with server alerts disabled
  and an upload protocol other than HTTPS POST.
- [x] Production firmware built from committed source in an isolated checkout.
- [x] Cellular HTTPS received real collector acknowledgements with Wi-Fi off.
- [x] An acknowledged scratch journal retained matching local bytes.
- [x] Retention boundaries and safe archive paths passed focused checks.
- [x] Physical buzzer audibility was confirmed.
- [x] Final installed build identity and image hash recorded below.
- [x] Three complete USB power disconnect/reconnect cycles passed a 90-second
  recording and cellular-upload check each; details below.
- [ ] Software-only reset behaviour and longer-term endurance remain healthy.
- [ ] Physical ignition/OBD collection verified in the car (USB only available).
- [x] USB SD export removed at the user request; local SD retention preserved.

## Final installed image

The production logger is installed with faster OBD polling and without USB SD export.

- Firmware source revision: `ac15b98`, with the existing production configuration.
- Build identity: `obd-fast-20260929`.
- Image size: 633,712 bytes.
- SHA-256: `f19f0d7a103e9c147ea4b09f47aa6af69e5d12bbf5c34c335cdb7d73adfee4e6`.
- PlatformIO upload completed and esptool verified the written image hash.
- The image has no FSD request parser, SD export responses or USB clock command.
- Twenty lifecycle/alarm checks passed after the polling changes.
- Wi-Fi remains disabled. No formatting branch is present in the normal image.
- The moving target is 250 ms: RPM is requested every cycle, two other core PIDs
  rotate each cycle, and one auxiliary PID is interleaved each cycle. The upload
  batch wait is capped at one second. ECU response latency limits real rates.
- USB bench boot confirmed the build identity and SD journal. One SD mount attempt
  failed at boot and correctly triggered the repeating alarm; local recording
  recovered and the alarm stopped. This is a recorded transient fault, not a clean
  mount on every boot.
- The same boot established cellular TLS/login and received repeated collector
  acknowledgements. It drained the prior SD backlog from 18,846 bytes to 107 bytes;
  new journal files were retained locally under the 14-day policy.
- No vehicle ECU was connected during this check, so actual OBD cadence, ECU load,
  and in-car collection remain unverified. The earlier three cold-power cycles
  and software-only reset caveat are recorded below.

### Three-cycle cold-power check

Each cycle was a physical USB power disconnect for 10 seconds followed by
reconnection. The installed build identity was verified after each boot. Every
cycle mounted the SD journal, created and grew a new CSV, and received cellular
collector acknowledgements, with no SD errors, recording warnings or beep
faults during the 90-second check.

| Cycle | CSV | Check duration | Cellular acknowledgements | Values accepted |
| --- | --- | ---: | ---: | ---: |
| 1 | `/DATA/78.CSV` | 90 s minimum | 48 | 23,715 |
| 2 | `/DATA/79.CSV` | 90 s minimum | 46 | 22,923 |
| 3 | `/DATA/80.CSV` | 90 s | 20 | 9,897 |
| **Total** | | | **114** | **56,535** |

Cycles 1 and 2 remained healthy until the next requested disconnect, for
approximately 176 seconds each. This verifies repeatable cold boot recovery on
the bench; it does not establish software-reset or in-car reliability.

## Cold-power recovery evidence before export removal

A complete USB power disconnect restored the existing card. The original
journal header and CRC were read successfully; the card was not formatted.
The 300-second observation received 105 acknowledgements for 52,461 values,
with zero SD driver failures, fault warnings or resets. The backlog fell from
3,152,323 to 2,725,164 bytes and the collector archive grew to 606,450 bytes.

A subsequent full directory check stalled collection. The export reader
rescanned prefixes and command retries delayed the collection loop. These
export paths are now removed. The card failed again after a later warm flash;
its underlying physical or driver fault remains unconfirmed. The previous
healthy interval does not establish long-term or in-car reliability.

## Grafana database-lock correction

The history database used DELETE journal mode while the indexer rewrote
active archives every five seconds. Grafana failed those reads immediately
with SQLITE_BUSY. The live plugin logs confirmed the reported failure.

The deployed schema now uses WAL. Reader connections remain read-only;
the directory mounts allow SQLite to create its WAL/shared-memory sidecars.
Grafana path options set `mode=ro`, `query_only(1)` and a 10-second busy timeout.
The history database was backed up and passed `PRAGMA quick_check` before the
change (478,121,984 bytes). Deployment used targeted `/srv/up.sh` updates.

The concurrency check failed before the change and passed after it. It checks
writer commits during an existing read, stable old-reader data, fresh new-reader
data, reopening after the writer exits, and rejection of SQL writes by readers.
The actual Trips dashboard refreshed with archive rows, and the plugin completed
128 history queries with zero database-lock errors after deployment.

The existing history test suite has one unrelated GNSS fixture failure: its
YYMMDD fixture disagrees with the firmware DDMMYY decoder. The committed
baseline produces the same timestamp mismatch. It was not changed here.
Some empty-data time-series panels still report a plugin conversion error;
that is separate from the corrected SQLite lock failure.

## Previous repeating-alarm image

The normal production logger is installed with the repeating fault alarm.

- Source revision: `757863e`, with the existing local production configuration.
- Build identity: `sd-alarm-repeat-20260929` (confirmed by USB `STATUS`).
- Image size: 636,144 bytes.
- SHA-256: `a393901ff3b95d84802ce7f68db3b31fbcac7b232cd7b85059d3e35ab0074851`.
- PlatformIO upload completed and esptool verified the written image hash.
- Live check: 19 alarm bursts in 105 seconds, spaced approximately 5.01 seconds
  apart. Nine cellular requests accepted 690 values during the same check.
- USB status confirmed the SD journal and CSV logger unavailable, Wi-Fi disabled,
  and retention set to 14 days. Successful uploads did not silence the alarm.
- The installed image does not include the format-provisioning branch or its
  message. The live log has no format-provisioning message. The SD card was not
  formatted during this review.
- Kieran confirmed that the warning on this image keeps repeating. The alarm
  cadence and physical audibility are both verified.

## Previous production image evidence

The normal production logger is installed. The diagnostic probe is removed
from the device. The modem radio was restored with `AT+CFUN=1` before upload.

- Source revision: `3be20d3`, with the existing local production configuration.
- Build identity: `cell-sd-guard-20260929` (confirmed by USB `STATUS`).
- Image size: 635,984 bytes.
- SHA-256: `caa788944fa882903758dc6cff691c968c076f6af7233b605b463a1789c54a36`.
- PlatformIO upload completed and esptool verified the written image hash.
- Final live status: local storage and journal unavailable; cellular HTTPS
  accepted 1,017 fresh values in 23 requests during the 225-second check.
  The fault warning sounded automatically. Seven remount attempts did not
  restore SD access. USB status remained available at 20, 70, 130 and 210 seconds.
- The collector file `20260929-020004.txt` grew to 6,276 bytes during this
  check. This is evidence of server persistence, not an SD copy.
- A failed boot reports zero cached pending bytes because the journal could
  not be opened. It does not establish that the real journal is empty. Its
  last successful physical size check was 3,681,396 bytes.

The final cold-power check passed for three minutes. Remaining physical checks
are another cable or USB port, and a known-good SD card to isolate the recurring
warm-reset fault. The car ignition/OBD check remains unavailable
because Kieran can use USB only. No card format or deletion of the original
journal was performed.

## Continuous sampling source change, 30 September 2026

This source change is not installed on the disconnected Model B. The installed
image evidence above remains historical evidence for `ac15b98`.

The sampler now has absolute 250 ms deadlines and no stationary throttle or
automatic parked standby. Separate tasks own OBD (including reconnect, VIN and
DTC requests), GNSS acquisition, MEMS reads, and SD logging/journaling. The
sampler copies short protected sensor snapshots and publishes a RAM slot.
External GNSS does not wait for the OBD co-processor lock. Internal ATGPS and
OBD share that lock, so an internal GNSS reading can become stale during an
OBD stall; its reported age keeps that visible. Clock checkpoints and SD
backlog statistics also run outside the sampling thread.

The recorder uses eight bounded PSRAM handoff slots only to pass a sample from
the sampler to the SD writer. They are not an outage spool: if the journal is
unhealthy or an append fails, the sample is released, counted as missed, and
never uploaded. Only a verified journal append makes a sample upload-eligible.
Finite SD capacity, power loss before commit, scheduling overload, and
hardware failure still prevent a physical zero-loss guarantee.

Latest successfully measured OBD values remain in every sample with individual
ages at `0x400 | pid`. GNSS, voltage, MEMS and signal strength have separate age
fields. The unchanged held GNSS fix time plus age preserves the 250 ms capture
timeline in the indexer. Diagnostic status records distinguish failed scan
attempts from known empty DTC lists. Unsupported/never-measured signals are
omitted instead of invented.

Validation commands:

```bash
python3 tools/check-sampling-boundary.py
python3 tools/check-device-lifecycle.py
python3 tools/check-sd-retention.py
python3 -m unittest discover -s collector -p history_indexer_test.py
pio run -e esp32dev
```

The host check compiles the real snapshot emitter, buffer serializer, queue
handoff methods and deadline scheduler with deterministic I/O. A complete
87-PID catalogue plus all DTC slots and conservative remaining sample fields
fits 2,048-byte RAM slots and the 8,192-byte journal frame limit. Element counts
are now 16-bit because a rich sample can exceed 255 fields. The production
PSRAM handoff is bounded to eight slots; the host stress fixture can model a
larger queue. Host scheduling checks cover 2,400 consecutive 250 ms
slots without accumulating simulated formatting time. These are source/host
checks, not ESP32 scheduling, SD endurance or physical ECU evidence.

Build artifact: `fullrate-20260930`, 635072 bytes.
SHA-256: `5e4603a342d9c1d82629e54ed972a9e37963f7845a469d84b1b2d3d47087ba7e`.
The source changes remain uncommitted. No firmware flash occurred. The two
collector decoder changes and age metadata were deployed to
`freematics-history` on bsociety using the targeted `/srv/./up.sh` path. The
deployed decoder replayed held GNSS fixes at `[0, 250, 500, 750, 1000]` ms,
reported held samples as anchored, published the age metric catalogue, and
preserved the 82,280 existing indexed samples. Remote deployment preserved
the pre-existing per-pass transaction policy rather than deploying unrelated
local indexer changes.

The live collector was also rebuilt for five PID ranges, enabling the `0x400`
age fields in the live API. Widening its native channel record exposed a
saved-layout mismatch. The original `channels.dat` was preserved and restored,
and compatibility loading was added for the previous four-range layout.
The local HTTP regression exercises checksum-acknowledged samples, held values
through the live API and raw archive, disconnect handling, and a restart with
legacy channel records. The live service is healthy and its `ZKUCALJ0` identity
was verified after recovery. Source changes preserve explicitly aged live
values on ECU disconnect while still clearing unaged legacy firmware values.
The deployed collector already retained disconnect values; only its PID table
range and channel loader changed during deployment.

```bash
make -C collector -B
python3 tools/check-collector-sampling.py
```

Live collector binary SHA-256:
`f9ac2f529940523a9a26596805c2f12ebb603ac888a597f056b31d1760a4b513`.
The full collector Python suite ran 62 checks: 55 passed, 4 optional checks were
skipped, and 3 Git mirror checks were blocked by the local Git identity proxy's
refusal to set the fixture bot identity. No Git identity guard was bypassed.

## SD failure investigation, 3 October 2026

The strongest historical evidence is for an intermittent card initialization
or mount failure, not a proven journal write failure. A recorded
`f_mount failed: physical drive cannot work` is FatFS `FR_NOT_READY`: it means
the physical drive was not ready for the volume mount. The SD core's SPI init
path already reports command-level failures (select/wait, GO_IDLE_STATE,
SEND_IF_COND, ACMD41/OP_COND, OCR, and block-length setup). On failed boots,
the application previously reduced this to `NO SD CARD` / `SD journal
unavailable`. Prior runs include repeated warm-reset/remount failures and
successful recovery after a full power disconnect. The same card's journal
header and CRC were subsequently read successfully without formatting.
Together these facts make persistent FAT corruption less likely, but do not
identify whether card readiness, power/contact/interface, reset timing, or a
card-controller fault is responsible. Do not label USB power as the cause.

During the separate 45-second live USB measurement on 3 October, journal
health (`0x08F`) remained 1, missed readings (`0x08E`) and rejected readings
(`0x097`) remained 0, and the device USB drop counter remained 0. This short
healthy window does not rule out an intermittent SD fault and provides no
evidence of an append/readback failure. The serial link had corrupt telemetry
records during that same window; that is a separate transport symptom.

The source adds `[SD-DIAG]` mount attempt/result/timing markers and
stage-specific journal errors: create/open, capacity, short header or frame
write, and readback open/position/header/content failures. On the current
indoor USB-only boot, all three 1 MHz mount attempts failed with `FR_NOT_READY`
(`physical drive cannot work`); each was followed by
`sdSelectCard(): Select Failed` after its 500 ms ready wait. During an 8-second
USB capture, 31 valid FT1 records remained spaced at 250 ms, USB drops and
restarts stayed at zero, the journal-health field stayed zero, and missed
readings rose from 199 to 229. This confirms live telemetry continues while
durable recording is unavailable and losses are counted rather than spooled.
The cached journal-bytes field was zero but is not proof of the physical
journal's size or contents. No format or destructive card test was run. This
recurrence localizes the failure to card initialization/response; its physical
cause remains unconfirmed. Compare after a full power disconnect before
changing the driver or card contents.

The firmware defines SD chip-select on GPIO5 and the SPI clock rate, but no
SD-card power-enable pin. Freematics' Model B product page links a schematic
that shows the TF-card interface on the 3.3 V rail with GPIO5 chip-select;
there is no software-controlled card power cycle available. This makes a full
device power disconnect the relevant next isolation step after the current
warm-reset failures, but does not by itself prove a card latch or power fault.
See the [official Model B schematic](https://freematics.com/dl/schematics_oneplus_r14_20190612.pdf).
