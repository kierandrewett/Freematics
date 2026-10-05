# SD recorder host verification — 2026-10-04

Repeatable command:

```sh
python3 tools/check-recorder.py
```

The fixture compiles and runs the production `journalSample()` path, SD
journal, and serializer against the repository's fake SD implementation. It
injects 650 ms per append while exercising the 250 ms sample deadline. The
sampler blocks on the in-flight SD commit; expired sample deadlines are
counted, and no completed samples wait in a RAM queue.

Recorded output:

```text
PASS: slow SD blocks sampling; one in-flight frame, skipped intervals counted, committed bytes verified; missed=80, journaled=40, opens=84
PASS: unavailable SD produces counted misses, no retained samples or upload candidates
PASS: SD recovery records new samples only; failed samples remain counted, never replayed
```

The test checks one-slot working memory, sample accounting, the deliberate
cadence loss under a 650 ms append, timestamp/order agreement between committed
journal frames and the CSV convenience logger, and no replay of samples from a
card-failure window. Recovery only records new samples. This is host simulation
evidence, not a physical-card endurance or warm-reset test. The recorded
device-side `FR_NOT_READY`/card-select failures occurred during mount and remain
unresolved; that points to card initialization or electrical response, not
evidence that this append/readback path failed.

## Mount-path follow-up

Source inspection of the pinned PlatformIO `espressif32@7.0.1` Arduino SD
implementation narrows, but does not settle, the mount fault:

- `SD.begin()` invokes `SPI.begin()` itself; the recorder's explicit
  `SPI.begin()` is redundant. In the pinned Arduino `SPIClass::begin()`, an
  already initialized `_spi` returns immediately, so the recorder call followed
  by the library call does not initialize the bus twice.
- When `sdcard_init()` has allocated a card object but `sdcard_mount()` fails,
  `SDFS::begin()` calls `sdcard_unmount()` and `sdcard_uninit()` before resetting
  its drive ID. `sdcard_uninit()` deregisters the FatFS disk driver and frees
  that card object. Thus the observed three-attempt path does not accumulate
  registered card objects between retries in this framework version.
- Card identification runs with a fixed 400 kHz SPI transaction, even though
  the configured post-initialization `SPI_FREQ` is 1 MHz. A lower configured
  transfer rate alone is therefore not a supported fix for the reported
  `FR_NOT_READY`/`Select Failed` mount errors.
- On initialization, the library clocks the card with chip-select high, then
  waits up to 500 ms for a nonzero ready response. It logs and ignores a
  timeout before sending `GO_IDLE_STATE`; the subsequent card-select path can
  then fail after its own 500 ms wait. This is consistent with a card that is
  still busy or electrically unavailable, but does not distinguish warm-reset
  card state, contact, supply, card damage, or reader hardware.

The next discriminating hardware run should preserve the card contents and
compare: (1) warm MCU reset with the card left inserted, (2) complete device
power removal/restart with the same card, (3) remove/reinsert then cold start,
and (4) a known-good compatible card on this device. Capture the existing
`[SD-DIAG]` attempt durations and card-select logs for each condition; measure
the card supply during startup if mount still fails. Do not format the card or
interpret USB bench voltage as vehicle supply. No test in this follow-up has
been performed on physical hardware.
