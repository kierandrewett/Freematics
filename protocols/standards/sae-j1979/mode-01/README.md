# SAE J1979 Mode 01

Mode 01 is the generic, emissions-oriented OBD-II live-data surface.  The
shared `obd_pids.h` file is the source of names, descriptions, units, and
polling priority.  Optional PIDs are represented as absent when an ECU does
not advertise them; absence is not converted to zero or treated as a fault.

Notable optional values include PID `0xA6` (vehicle odometer).  A profile may
request it only after supported-PID discovery and must retain the raw response
and ECU identity alongside the decoded value.
