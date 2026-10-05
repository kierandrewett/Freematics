# Telemetry ingest reliability contract

This documents the capture-to-history path and the failure windows that remain.

## Current state

* SD journal records use FQJ2 capture identity: one persistent 64-bit session
  and a wrapping uint32 sequence are assigned once before journaling; the CRC
  covers the identity and frame. Existing FQJ1 records remain readable. The
  upload cursor advances only after an accepted response, so lost responses
  replay the same IDs.
* HTTPS replay uses FQI1 envelopes. The collector validates framing and CRC,
  durably stores exact payload bytes under device/session/sequence, then ACKs
  the exact received sequence list. Same-ID/same-payload retries are
  idempotent; same-ID/different-payload conflicts return 409. Invalid records
  fail with 400, and inbox durability failures return 503 without an ACK.
  The current durable inbox implementation is POSIX-only; the Windows build
  stub returns 503 and must not be used as an FQI1 ingest target.
* The inbox is authoritative; the legacy text archive is not duplicated for
  FQI1 records. The Python history indexer projects inbox records into SQLite
  using stable `fqi-<session>` trip IDs. Valid device UTC requires both PIDs
  `0x90` and `0x91`; collector receipt time is stored separately and never
  substitutes for capture time. Sequence holes and monotonic-time intervals
  are reported independently in Grafana.
* The 3.75 GiB FAT32 journal bound or a failed card can still cause missed
  collection cycles. SD builds keep only one in-flight capture in working
  memory and synchronously verify its journal append before starting another;
  a slow append can overrun the 250 ms cadence, while an append failure counts
  the capture as missed and never makes it an upload candidate. The missed-
  readings counter and historical interval/sequence views are evidence of
  losses, not substitutes for absent data. A sequence hole indicates a
  journaled-ID gap but does not by itself attribute loss to ECU polling, SD
  latency, reset timing, or upload transport.

## Current wire and projection contract

Each FQI1 record preserves the existing ordered PID frame as raw evidence. Its
envelope is:

| Field | Rule |
| --- | --- |
`FQI1,<16-hex session>,<uint32 sequence>,<payload byte length>,<CRC32>\n`
followed by exactly that many payload bytes and a newline. Sequence `0` is
valid at uint32 rollover. One upload batch contains a single session and may
contain sequence holes; its ACK lists precisely the accepted sequence values,
not a contiguous prefix or a parsed-field count. The device verifies the
session and full ACK list before advancing the SD cursor.

The Linux collector publishes inbox files atomically and syncs each file and
its containing directory before ACK. The indexer keeps corrupt inbox files for
investigation and excludes them from SQLite. It is idempotent when new files
arrive and recomputes the session projection when the inbox contents change.
Legacy frames continue through the existing archive path with unknown capture
identity; do not infer identity from `millis()`, PID values, or upload order.

## Remaining reliability evidence

## Operational evidence needed before a no-missed-readings claim

* A device-side accepted, pending, and missed reading count with card free
  space and oldest pending age; notify on any missed reading and persistent
  backlog growth.
* Server-side unique accepted count, duplicate replay count, inbox age, and
  projection lag, with alerts independent of the vehicle connection.
* A controlled offline drive, reboot with backlog, lost-ACK replay, full-card
  scenario, and host restart during ingestion. Reconcile device IDs and counts
  with inbox records and Grafana history after each run.

Finite hardware and physical card failure prevent an unconditional guarantee
that every intended collection cycle survives. The system must retain every
successfully journaled reading until an independently durable server ACK, and
surface any cycle it could not journal immediately when a channel is available.
