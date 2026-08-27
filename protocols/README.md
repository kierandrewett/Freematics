# Vehicle protocol profiles

This tree is the extension boundary for vehicle-specific decoding.  The
collector always records the original PID fields first; a profile can add
names or derived values only when its identity and transport prerequisites
are satisfied.

```text
protocols/
  standards/                 # public standards and wire semantics
    sae-j1979/mode-01/       # generic OBD-II Mode 01 catalogue
    iso-15765/                # CAN/ISO-TP transport
    iso-14229-uds/            # diagnostic application layer
  manufacturers/
    opel-vauxhall/
      gmlan/                  # GM-specific candidate services
      corsa-d/2006-2014/      # model-generation profile
```

Profiles are data and documentation, not permission to transmit arbitrary CAN
or UDS commands.  New manufacturer work must be:

1. identity-gated (VIN/model/engine ECU evidence is recorded first),
2. read-only by default (no writes, coding, security access, or actuator tests),
3. capability-gated (supported services/PIDs are discovered before polling),
4. explicit about evidence quality and unknown values.

The generic archive schema keeps `fields` and raw frame bytes unchanged.  The
human-readable `metrics` projection uses the namespaces in this tree where a
profile is active; it never replaces raw evidence.

`collector/vehicle_profiles.py` validates these profiles and releases only
identity-matched, read-only candidate metadata. `collector/vendor_diagnostics.py`
classifies captured UDS/GMLAN positive and negative response envelopes without
opening a transport or issuing retries.
