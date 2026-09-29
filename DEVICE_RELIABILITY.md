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
- An SD append fails, a record is partial, or a cursor is invalid. Keep the original bytes and the RAM sample.
- A failed existence check is followed by a truncating journal create. Use exclusive creation so existing bytes cannot be erased.
- Repeated I/O attempts on a failed card stall collection and USB diagnostics. Keep the cached backlog until scheduled recovery.
- Fresh RAM readings prevent an older SD backlog from ever being replayed.
- USB bench power enters parked standby before the existing SD backlog has uploaded.
- Uploaded data disappears from the local archive.
- An unbounded archive retention scan stalls fresh collection for seconds as the directory grows.
- Retention removes recent data, data of unknown age, the active file, or unacknowledged journal data.
- A computer reads while the device writes. Export complete bounded responses and do not expose writes.
- USB disconnects, a serial response is damaged, or the firmware restarts. Return an I/O error and reconnect.
- ESP32 light sleep loses incoming USB serial requests while parked. Keep serial input available on USB power.
- A malformed export path accesses credentials or changes device configuration.
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

## USB filesystem requirement

The existing USB socket terminates at a CH340 serial bridge. The attached
ESP32-D0WDQ6 has no native USB device controller. ESP32-S2/S3 USB mass-storage
examples cannot change the descriptors or function of this bridge.
A real mass-storage filesystem through this socket requires a hardware change.
See [Espressif USB support](https://docs.espressif.com/projects/esp-faq/en/latest/software-framework/peripherals/usb.html).

`tools/freematics_sd.py` is a serial diagnostic reader. It is not USB mass
storage. No FUSE mount or automatic host mount is installed.

## Recovery and retention behaviour

A complete sample must reach a verified SD journal write before its RAM slot
is released. A failed POST retains the batch for retry. Only an exact collector
acknowledgement advances the saved cursor. A login or ping cannot silence the
missing-data alarm. SD recovery never formats the card.

When the card fails, fresh samples remain in RAM and can upload over cellular.
They cannot have an SD copy until the card works again. RAM does not survive
power loss. The remaining SD fault therefore prevents acceptance of reliable
recording and the requested local retention on this unit.

Once all journal records are acknowledged, the journal is renamed into
`/DATA/<id>.BIN` and retained locally. CSV and binary archives expire after
14 days. Files with unknown creation time receive a full new 14-day window.
Unacknowledged data is never removed by retention. A failed existence check
cannot truncate an existing journal because creation uses `O_EXCL`.

On USB power, an existing backlog keeps the upload task active beyond the
parked idle cutoff. USB serial input also remains available during parked
idle. Vehicle power retains the normal parked sleep policy.

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
- [ ] Repeated reboot and sustained SD recording/upload remain healthy.
- [ ] Physical ignition/OBD collection verified in the car (USB only available).
- [ ] Real USB mass storage available (requires different hardware).

## Final installed image

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
- The buzzer waveform was already heard on the previous image. Confirmation
  that Kieran hears the new repeated warning is pending.

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

Remaining physical checks are a full power disconnect, another cable or USB
port, and a known-good SD card. The car ignition/OBD check remains unavailable
because Kieran can use USB only. No card format or deletion of the original
journal was performed.
