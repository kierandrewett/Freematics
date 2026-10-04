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
