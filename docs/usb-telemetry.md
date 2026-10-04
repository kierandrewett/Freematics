# Freematics USB telemetry (FT1)

The Model B USB serial connection is a passive telemetry feed, not an ELM327
command channel. It emits one newline-terminated record for each sampler
frame. Debug text may appear between records; consumers should accept only
validated `@FT1,` records and tolerate partial serial reads.

The envelope is:

```text
@FT1,<boot-id>,<capture-ms>,<utc-valid>,<capture-utc-ms>,<dropped>,<metadata>|<serialized-sample>\n
```

`capture-ms` is the device monotonic capture time. `capture-utc-ms` is Unix
epoch milliseconds and is valid only when `utc-valid` is `1`; otherwise it is
zero and must not be treated as a wall-clock timestamp. `dropped` is the
cumulative USB telemetry queue drop count. The serialized sample retains
measurement values and per-measurement acquisition ages, including waveform
fields; it is not reconstructed from upload receipt time.

`metadata` begins with the comma-separated supported Mode 01 PID list. Optional
semicolon-delimited fields follow:

* `vin=` is the validated 17-character VIN.
* `cal=` is uppercase hexadecimal encoding of the first supported Mode 09
  calibration-ID record (decoded text is at most 16 bytes).
* `ecu=` is uppercase hexadecimal encoding of the first supported Mode 09 ECU
  name record (decoded text is at most 20 bytes).

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

The checksum on the serialized sample must be validated independently of the
FT1 envelope metadata. A syntactically valid USB record is not proof that the
ECU supports a PID or that an ECU transaction succeeded unless the associated
support/status fields say so.
