# Local firmware fault simulator

Run from the repository root:

```sh
python3 tools/emulator/run.py --report /tmp/freematics-emulator.json
```

Requires Python 3 and a C++ compiler (`c++`). No device, credentials or network are required.
Use `--strict` to return a failure status for any firmware issue. The default returns success when the simulator runs
and the normal scenarios pass. It reports fault scenarios that fail as `ISSUE`.

The runner compiles the complete current `FreematicsOBD.cpp` against a dummy `CLink` bridge.
The bridge returns scripted ECU replies and advances a virtual clock. Timeouts do not cause a real wait.
The queue scenarios compile the current `CBufferManager::getOldest` method from `teleclient.cpp`.
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

## Coverage limits and next steps

This runs the actual OBD decoder and queue selection method. It does not boot the ESP32 image or run FreeRTOS tasks.
It does not prove sensor scheduling, I2C behaviour, SD persistence, modem operation or power-loss recovery.
Existing `tools/check-sampling-boundary.py` and `tools/check-collector-sampling.py` cover additional sampling and
local collector paths. Their mocks do not replace a hardware run.

A next stage can add a native firmware runtime with dummy GNSS, IMU, storage and HTTP inputs. It should inject delayed
replies, card failures, dropped acknowledgements, restarts and clock changes, then compare captured and received samples.
Reuse production logic at each boundary. Do not implement a separate copy of the firmware behaviour.

For CPU and task execution, Espressif QEMU can boot an ESP32 flash image. The Freematics bridge, modem and sensors
need models or an explicit simulation build with fake hardware drivers. Wokwi also supports ESP32 firmware and custom
chips. Neither option supplies a complete Freematics ONE+ Model B model without further work.

Sources:

- [Espressif ESP32 QEMU guide](https://github.com/espressif/esp-toolchain-docs/blob/main/qemu/esp32/README.md)
- [Wokwi ESP32 simulation](https://docs.wokwi.com/guides/esp32)
- [Wokwi custom chips](https://docs.wokwi.com/chips-api/getting-started)

## Issues found on 2026-09-30

The simulator reproduced four fault scenarios on the current working tree:

- `41 0C 1A` is accepted as 6.5 RPM. RPM requires two data bytes. The decoder does not check the payload length.
- `41 0D ZZ` is accepted as zero speed. Invalid hexadecimal data becomes zero and the read reports success.
- Queue selection chooses timestamp 10 before `0xffffff00`, although the latter was captured first.
- An entry at timestamp `0xffffffff` is never selected by `getOldest` because its initial sentinel has the same value.

Source inspection found three further risks. The simulator does not yet exercise these paths:

- `ICM_42627::readAccelData`, `readGyroData` and `readTempData` ignore a failed `readBytes` call and read uninitialised
  local arrays. The public `read` method returns true. The worker can therefore mark invalid sensor data as fresh.
- A partial or corrupt journal record stops replay. Mount recovery resets the flags but does not repair or quarantine
  the damaged bytes. This preserves evidence, but the same record can stop replay again and leave new samples in finite RAM.
- The MEMS worker replaces one snapshot about every 20 ms. The sampler records the latest snapshot every 250 ms.
  This does not preserve every sensor acquisition. A short acceleration peak can occur between recorded snapshots.

These findings remain unfixed. The decoder and queue failures have repeatable host evidence. The sensor and journal
findings need fault injection before a fix can be validated. An abrupt power loss also destroys samples that are still
in RAM while they wait for the recorder. This simulator does not measure that persistence window.
