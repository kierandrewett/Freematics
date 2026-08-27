# ISO 15765 CAN / ISO-TP

This is the transport layer for CAN-based diagnostic traffic.  The safe
sequence is: identify the ECU, discover supported services/PIDs, then poll
only advertised read operations.  Functional requests and physical response
addresses are vehicle/ECU dependent; this directory intentionally contains no
hard-coded write or security-access operation.
