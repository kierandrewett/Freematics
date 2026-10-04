# Freematics USB telemetry (FT2, with FT1 compatibility)

The Model B USB serial connection is a passive telemetry feed, not an ELM327
command channel. It emits one newline-terminated record for each sampler
frame. Current firmware emits FT2. Debug text may appear between records;
consumers should accept only validated `@FT2,` records and tolerate partial
serial reads. The dashboard also accepts legacy FT1 records from older firmware.

The envelope is:

```text
@FT2,<boot-id>,<capture-ms>,<utc-valid>,<capture-utc-ms>,<dropped>,<metadata>|<serialized-sample>*<crc32>\n
```

The FT2 CRC is CRC-32/ISO-HDLC (the standard `zlib.crc32` result), encoded as
eight uppercase hexadecimal digits. It covers the exact ASCII bytes starting
with `@FT2` and ending at the final serialized-sample byte, immediately before
`*`. Capture clocks, UTC validity, USB drop count, support map, VIN,
identity/raw metadata, device ID, measurement values, ages, and waveforms are
therefore integrity-checked together. A CRC failure invalidates the entire FT2
record; consumers must not fall back to parsing its contents as FT1.

Legacy FT1 keeps its original one- or two-digit additive checksum over only
the serialized sample. FT1 metadata therefore has weaker integrity and is
accepted for compatibility with older firmware.

`capture-ms` is the device monotonic capture time. `capture-utc-ms` is Unix
epoch milliseconds and is valid only when `utc-valid` is `1`; otherwise it is
zero and must not be treated as a wall-clock timestamp. `dropped` is the
cumulative USB telemetry queue drop count. The serialized sample retains
measurement values and per-measurement acquisition ages, including waveform
fields; it is not reconstructed from upload receipt time.

USB output uses a fixed two-record queue and a separate, low-priority writer
task. Every new sample replaces an older snapshot that is waiting to be sent;
the record already being written is left unchanged. The drop counter increases
for replaced or rejected records, avoiding a stale FIFO backlog while the host
is slow. UART startup reserves a TX ring larger than the maximum FT2 line so
the writer enqueues each record as a bounded operation instead of holding the
shared UART lock for the line's full wire time. If that ring cannot initialize,
FT2 streaming stays disabled while acquisition, journalling, and uploads
continue.
The FT2 writer is separate from the sampler, SD-journal, and upload paths; the
queue handoff itself never waits for the laptop. Debug messages may also be
written by firmware tasks. This bounds the FT2 firmware handoff; the USB
adapter/host may still lose bytes, so readers must validate checksums and
recover at the next complete record. The fixed-cadence sampling functions do
not write to `Serial`; missed frames, queue drops, and power phase remain
observable through telemetry counters/fields without a synchronous debug
write in the sampling path.

`metadata` begins with the comma-separated supported Mode 01 PID list. Optional
semicolon-delimited fields follow:

* `vin=` is the validated 17-character VIN.
* `cal=` is uppercase hexadecimal encoding of the first supported Mode 09
  calibration-ID record (decoded text is at most 16 bytes).
* `ecu=` is uppercase hexadecimal encoding of the first supported Mode 09 ECU
  name record (decoded text is at most 20 bytes).
* `raw=` is an optional comma-separated list of `PP:HEX` entries, where `PP`
  is a two-digit Mode 01 PID and `HEX` is the ECU response data bytes in
  uppercase hexadecimal. It carries every PID in the shared Mode 01 catalogue
  once that reading has been successfully acquired. Its byte width follows the
  dashboard's Mode 01 definition, preserving full status/compound responses
  rather than only normalized values. The bytes accompany the ordinary sample
  and come from the existing ECU poll, not a second dashboard poll. Entries
  without a valid response are omitted. Existing clients may ignore this field.

Metadata fields are appended in the order `vin`, `cal`, `ecu`, `raw`, omitting
any unavailable field. Consumers must validate each raw PID's defined byte
width and reject duplicate, malformed, or unknown raw entries. The metadata
buffer is sized to 2 KiB and tested with all catalogue entries at the maximum
four-byte width. A firmware-side metadata/line overflow rejects that USB
record and increments the dropped record count; it does not emit a truncated
list as valid data. Debug log lines may still occur between telemetry records and
are not part of this metadata format.

The firmware reads Mode 09 PID 00 once after OBD initialization and queries
PIDs 04 and 0A only when their support bits are positively advertised. Those
bounded reads use the existing OBD-owner task; they do not add an ECU polling
loop or alter the 250 ms sampler cadence. Unsupported, timed-out, malformed,
or non-printable identity data is omitted rather than presented as a successful
read. Older firmware may omit `cal` and `ecu`; older FT1 frames without those
fields remain valid.

The current implementation and host fixtures can be checked with:

```sh
g++ -std=c++11 -Wall -Wextra -Werror tools/test_mode09_identity.cpp -o /tmp/test_mode09_identity
/tmp/test_mode09_identity
```

For FT2, validate the whole-record CRC before using any metadata or sample
field. For FT1, validate the legacy sample checksum and treat envelope metadata
as unauthenticated. In either version, a valid checksum is not proof that the
ECU supports a PID or that an ECU transaction succeeded unless the associated
support/status fields say so.

The `;raw=` field supplements, but does not replace, normalized values,
per-reading ages, and support/status fields in the serialized sample. Missing
raw metadata must not be interpreted as a zero-valued reading. To run the
firmware-side raw-width/metadata fixture and the dashboard parser tests:

```sh
python3 tools/test_usb_raw_metadata.py
python3 tools/test_usb_telemetry_queue.py
(cd ../obd && cargo test freematics_usb)
```
