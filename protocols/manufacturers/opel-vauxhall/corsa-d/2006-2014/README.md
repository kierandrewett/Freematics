# Vauxhall Corsa D (2006–2014)

This generation profile is the starting point for the user's 2012 Corsa D.
The engine variant still needs to be identified from the VIN/ECU: the UK range
contains several petrol and diesel engines with different module behaviour.

The OBD connector exposes more than one CAN bus on some model years.  The
profile records bus/pin evidence and candidate services, but the device must
prove which bus and ECU answered before a manufacturer-specific decoder is
activated.  Standard SAE J1979 Mode 01 polling remains the safe baseline.

The disabled GMLAN candidate set records read-only identifiers `0x90`
(VIN), `0x92` (supplier), `0x97` (system or engine type), `0x9A`
(diagnostic data), `0xB0` (ECU address), `0xDE` (GMLAN identity), and
`0xDF` (ECU odometer). These names come from the
[Scapy GMLAN registry](https://github.com/secdev/scapy/blob/master/scapy/contrib/automotive/gm/gmlan.py);
they are not confirmed Corsa values. Enable one only after a matching
positive response and raw capture exist for the installed ECU.
