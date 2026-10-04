# Mode 02 freeze-frame telemetry

The logger passively publishes selected ECU Mode 02 frame-0 values after a
stored Mode 03 DTC is found. This is a device-side acquisition: the laptop
dashboard does not send commands over the telemetry USB connection.

## Wire fields

- `0x200 + PID`: decoded value for the corresponding SAE Mode 01 PID, sourced
  from Mode 02 frame 0. It is separate from the live `0x100 + PID` field.
- `0x363`: milliseconds since the logger's first successful read in the
  current frame-0 sweep. This is a device read age, not the ECU's fault-time
  timestamp; the Mode 02 response does not provide that timestamp.
- `0x364`: status (`0` not captured, `1` values read, `2` no Mode 02 values
  returned, `3` capture in progress).
- `0x365`: raw 16-bit DTC value that triggered the current sweep.

The current sweep requests common powertrain PIDs (engine load, coolant,
trims, MAP, RPM, speed, timing, intake temperature, MAF, throttle, fuel level,
and control-module voltage). Mode 01 support bitmaps are not treated as Mode 02
support declarations. Each Mode 02 request is probed directly.

## Scheduling and limitations

Diagnostic work only runs when fresh RPM and speed both confirm the vehicle is
stopped. A frame-0 sweep advances one PID request per scheduler turn and only
when the next RPM/speed deadline leaves enough time for a bounded serial wait.
Live acquisition takes precedence. The ECU bridge is still a single
request/response link, so a slow Mode 02 ECU response can consume some of that
reserved time; this should be measured against a real ECU before claiming the live PID
freshness target is unchanged.

Values are the ECU's retained snapshot, not necessarily present-day readings.
Only the logger read age and the containing telemetry record's device capture
time are known. When a successful stored-DTC scan finds no codes, the cached
freeze-frame fields are cleared. No DTC clear or freeze-frame write command is
issued by the passive dashboard.

Host parser fixtures and the Freematics collector catalogue tests cover
formatting, status, age, and malformed responses. Hardware validation still
requires an ECU with a stored DTC and a real Mode 02 response.
