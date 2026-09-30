# Telemetry ingest reliability contract

This is the implementation plan for moving from the current at-least-once SD
journal to an idempotent capture-to-archive path. It records the failure windows
that still matter after the collector's disk-sync acknowledgement fix.

## Current state

* The device writes complete, CRC-checked frames to `/QUEUE.BIN` and advances a
  separate cursor only after an accepted HTTP response. A lost response can
  replay frames. The journal has no durable reading identity, so the collector
  cannot distinguish a replay from a new reading.
* The collector now acknowledges only after the raw trip archive is flushed
  and `fsync` succeeds. A failed open, write, flush, or sync returns HTTP 503.
  An ACK after a successful write can still be lost on the network, causing a
  duplicate append. A newly created file or directory also needs directory
  sync for a complete host-power-loss guarantee.
* A collector trip ID is based on receipt time. A backlog captured before a
  device reboot can be written into a later collector trip. Where GNSS UTC is
  absent, the old frame contains no durable capture-time or boot identity to
  repair that attribution reliably.
* The 3.75 GiB FAT32 journal bound, failed card, or full RAM queue can still
  cause missed collection cycles. The missed-readings counter is evidence of
  those cycles, not a substitute for the absent data.

## Next wire format

Keep the existing PID payload as the raw evidence. Wrap each complete reading
in an envelope with these fields before it enters the SD journal:

| Field | Rule |
| --- | --- |
| `device_id` | Stable device identity, checked against the upload credential. |
| `capture_id` | Persistent 128-bit random recording-session ID, created before the first sample and retained across upload retries. |
| `sequence` | Monotonic 64-bit reading number within `capture_id`, assigned once before journaling. |
| `monotonic_ms` | Device tick at collection, never replaced by upload time. |
| `capture_utc_ms` | GNSS/network UTC when known; otherwise null with time-source and uncertainty fields. |
| `payload` | Complete original ordered PID frame, including repeated PIDs. |
| `payload_crc32` | Corruption check over the exact payload bytes. |

Store the envelope itself on SD so reboot/retry cannot create a new ID for the
same reading. A new recording session after reboot gets a new `capture_id`;
queued readings retain their original one. Batch uploads preserve order but an
ACK names the accepted `(capture_id, sequence)` records, not merely a count of
parsed values. Unknown protocol versions must fail closed without advancing
the SD cursor.

## Server acceptance boundary

1. Authenticate the device and reject malformed envelopes before any ACK.
2. Insert the exact envelope bytes into a durable inbox keyed by
   `(device_id, capture_id, sequence)`. A repeated key with the same bytes is
   an idempotent success; the same key with different bytes is an integrity
   fault and must be reported.
3. Commit and sync the inbox transaction before returning the accepted IDs.
   Retain an explicit storage-error response so the device keeps its copy.
4. Build trip archives, SQLite history, Grafana projections, and AI evidence
   from that inbox. These are rebuildable outputs; their failure must not make
   the inbox claim an absent reading, and their lag must be visible.

During migration, accept legacy frames through the current archive path and
mark their identity and capture time as unknown. Do not infer uniqueness from
`millis()`, PID values, or upload order. Introduce the inbox server first, then
flash the envelope-writing firmware, then switch downstream projections to the
inbox after historical import and count reconciliation.

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
