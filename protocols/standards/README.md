# Public standards

Standards are kept separate from manufacturer profiles so a future vehicle can
reuse the same OBD-II, CAN, and ISO-TP implementation.  The current generic
catalogue is sourced from the repository's `obd_pids.h` X-macro and emits
standard Mode 01 fields on wire PID `0x100 + pid`.

- SAE J1979: diagnostic services and standard OBD-II PIDs.
- ISO 15765-4: CAN diagnostic transport and supported-PID discovery.
- ISO 14229 (UDS): application-layer diagnostics over a transport such as
  ISO-TP.  UDS access is not enabled by this profile tree yet.
