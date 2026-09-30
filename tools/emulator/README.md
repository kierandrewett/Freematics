# Local firmware fault simulator

Run from the repository root:

```sh
python3 tools/emulator/run.py --strict --report /tmp/freematics-emulator.json
python3 tools/emulator/run.py --strict --collector --report /tmp/freematics-emulator-collector.json
```

Requires Python 3 and a C++ compiler (`c++`). No device, credentials or network are required.
The `--collector` run also requires `make` and the collector's C build dependencies. It builds the collector and starts
a temporary HTTP server on a local port with dummy credentials. It does not contact the deployed collector.
Use `--sanitize` to enable AddressSanitizer and UndefinedBehaviorSanitizer for the native firmware paths.
Use `--compiler clang++` if the default compiler has no sanitizer runtime installed.
Use `--strict` to return a failure status for any firmware issue. The default returns success when the simulator runs
and the normal scenarios pass. It reports fault scenarios that fail as `ISSUE`.

The runner compiles the complete current `FreematicsOBD.cpp` against a dummy `CLink` bridge.
The bridge returns scripted ECU replies and advances a virtual clock. Timeouts do not cause a real wait.
The queue scenarios compile the current `getOldest` and `getNewest` methods from `teleclient.cpp`.
The journal scenarios compile byte-identical copies of `telequeue.cpp` and `telequeue.h` with fake hardware headers.
The IMU scenarios load the production ICM-42627 class declaration, scales and read methods with a fake byte-transfer method.
The drive uses the production wire checksum and packet finalisation methods.
The report includes source hashes, the command, compiler, environment, results and coverage limits.

## Failure modes defined before implementation

- Normal RPM and speed replies must decode to the expected values.
- No response must fail, consume the receive timeout and increase the error count.
- A successful reply after an outage must reset the error count.
- `NO DATA`, another PID's response and an empty payload must fail.
- Truncated and invalid hexadecimal payloads must fail, rather than produce a fresh value.
- A failed read must leave the caller's previous value unchanged.
- A drive with changing RPM must retain the correct values across an ECU outage.
- A valid trouble-code response must retain its diagnostic code and status.
- Queue selection must retain FIFO order when the 32-bit millisecond clock wraps.
- A queue entry at timestamp `0xffffffff` must remain selectable.
- Newest selection must work across rollover and at timestamp zero.
- Two-byte and four-byte PIDs must reject missing required bytes.
- Valid lowercase hexadecimal bytes and extra spaces must decode correctly.
- Journaled readings must survive a restart before acknowledgement.
- A lost acknowledgement must replay the same records, without skipping them.
- A failed acknowledgement checkpoint must replay from the last saved cursor.
- A failed or partial append must not claim success or silently discard existing records.
- Missing storage must not become healthy or accept a sample.
- A corrupt record or cursor must not advance acknowledgement past unknown data.
- Journal rotation must retain accepted data, including when the rename fails.
- A dummy drive must reach the real local HTTP collector after an outage and restart.
- A failed acceleration, gyro or temperature read must reject the whole IMU snapshot and keep caller outputs unchanged.
- Successful IMU reads must retain the existing scale conversion.

## Coverage limits and next steps

This runs the actual OBD decoder, queue selection, journal and IMU read methods. It does not boot the ESP32 image or
run FreeRTOS tasks. The fake SD bytes survive a simulated device restart. This does not model filesystem caches,
physical flush guarantees, card controller behaviour or power interruption during a physical SD write.
It does not prove sensor scheduling, physical I2C behaviour, modem operation or hardware power-loss recovery.
Existing `tools/check-sampling-boundary.py` and `tools/check-collector-sampling.py` cover additional sampling and
local collector paths. Their mocks do not replace a hardware run.

The drive creates 240 journaled readings with synthetic 250 ms timestamps while the network is unavailable,
restarts, and then replays the journal. It does not use the production sampling task to create these timestamps.
It deliberately loses 12 acknowledgements after acceptance. The collector run verifies all 240 timestamps in the raw
archive and the final RPM through the live API. It checks each response's exact field count before acknowledgement.
Retries create duplicate requests. This proves retention for this scenario, not exactly-once ingestion.

A next stage can run the production sampler and recorder together with dummy GNSS, IMU, storage and HTTP inputs.
That stage must test task ordering, contention and the window between capture and journal persistence.
Use production logic at each boundary. Do not implement a separate copy of the firmware behaviour.

For CPU and task execution, Espressif QEMU can boot an ESP32 flash image. The Freematics bridge, modem and sensors
need models or an explicit simulation build with fake hardware drivers. Wokwi also supports ESP32 firmware and custom
chips. Neither option supplies a complete Freematics ONE+ Model B model without further work.

Sources:

- [Espressif ESP32 QEMU guide](https://github.com/espressif/esp-toolchain-docs/blob/main/qemu/esp32/README.md)
- [Wokwi ESP32 simulation](https://docs.wokwi.com/guides/esp32)
- [Wokwi custom chips](https://docs.wokwi.com/chips-api/getting-started)

## Issues found on 2026-09-30

The first simulator run reproduced four fault scenarios:

- `41 0C 1A` was accepted as 6.5 RPM. RPM requires two data bytes.
- `41 0D ZZ` was accepted as zero speed.
- Queue selection chose timestamp 10 before `0xffffff00`, although the latter was captured first.
- An entry at timestamp `0xffffffff` was never selected by `getOldest` because its initial sentinel had the same value.

Further scenarios reproduced truncated voltage and oxygen reads, newest-entry rollover failures, and failed I2C reads
reported as successful. These decoder, queue and ICM-42627 error-handling faults are now fixed. Signed queue comparisons
assume queued timestamps span less than half the clock period, about 24.9 days.

Remaining risks:

- A partial or corrupt journal record stops replay. Mount recovery resets the flags but does not repair or quarantine
  the damaged bytes. This preserves evidence, but the same record can stop replay again and leave new samples in finite RAM.
- The MEMS worker replaces one snapshot about every 20 ms. The sampler records the latest snapshot every 250 ms.
  This does not preserve every sensor acquisition. A short acceleration peak can occur between recorded snapshots.

The simulator verifies that a damaged record cannot be skipped. It does not repair the damaged journal.
An abrupt power loss also destroys samples that are still in RAM while they wait for the recorder.
This simulator does not measure that persistence window.
