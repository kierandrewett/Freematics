# Recording and waveform replay evidence — 2026-10-04

The repeatable host replay now starts with a full frame serialized by the
production `CStorageRAM` formatter. The replay validates the frame checksum,
normalizes the record to the SD-journal delimiter, then runs production journal
and upload-batch code against simulated SD bytes and a local collector. The
first collector acknowledgement is deliberately lost, so the device replays
the stored batch after restart.

The checked-in fixture is
[`waveform-frame.fixture`](../tools/emulator/fixtures/waveform-frame.fixture).
The normal and Clang AddressSanitizer/UndefinedBehaviorSanitizer reports each
record 62 passing scenarios with no issues or errors:

- [`waveform-replay-20261004.json`](../tools/emulator/waveform-replay-20261004.json)
  SHA-256: `8daeb3928399e078c99056fe39420e8820ab4d0887f08cf5d9dc0c1c9621d2e5`
- [`waveform-replay-sanitized-20261004.json`](../tools/emulator/waveform-replay-sanitized-20261004.json)
  SHA-256: `8d3d08d34a3539b09c8680cc78884346fcea219cc552278b439b3038fe3efc39`

The indexed result retains repeated waveform field order and acquisition
offsets through the lost-ack replay. The 9.8 V voltage dip remains in its
original sample slot; the read-only sensor-waveform query returns the expected
voltage and motion points, while quality counts each acquisition once despite
the duplicate upload.

This is host evidence only. The simulated SD does not establish physical card
flush durability or power-loss behaviour, and it does not measure ECU polling,
sensor cadence, cellular transfer, or actual Grafana ingestion. It cannot
replace Model B and vehicle validation.
