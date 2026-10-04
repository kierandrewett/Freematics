# SD recorder host verification — 2026-10-04

Repeatable command:

```sh
python3 tools/check-recorder.py
```

The fixture compiles and runs the production sampler-to-recorder path, SD
journal, and serializer against the repository's fake SD implementation. It
uses the configured eight-slot handoff and injects 650 ms per journal append
while sampling continues at the configured 250 ms interval.

Recorded output:

```text
PASS: 250 ms sampler interleaved with slow SD; configured capacity=8, peak held=6, missed=0, journaled=120, opens=98
PASS: full-SD failure holds at most configured capacity and counts/discards unjournaled samples
PASS: recovered SD journals new samples only; failed samples remain counted, never replayed
```

The test checks sample accounting, the handoff bound, and timestamp/order
agreement between committed journal frames and the CSV convenience logger. In
the injected card-failure phase, new samples are counted as missed rather than
retained for upload; after recovery, only successfully journaled samples are
replayable. This is host simulation evidence, not a physical-card endurance or
warm-reset test. The recorded device-side `FR_NOT_READY`/card-select failures
occurred during mount and remain unresolved; that points to card initialization
or electrical response, not evidence that this append/readback path failed.

## Mount-path follow-up

Source inspection of the pinned PlatformIO `espressif32@7.0.1` Arduino SD
implementation narrows, but does not settle, the mount fault:

- `SD.begin()` invokes `SPI.begin()` itself; the recorder's explicit
  `SPI.begin()` is redundant, but no evidence yet shows it causes the failure.
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
