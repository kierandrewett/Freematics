# Vauxhall Corsa D (2006–2014)

This generation profile is the starting point for the user's 2012 Corsa D.
The engine variant still needs to be identified from the VIN/ECU: the UK range
contains several petrol and diesel engines with different module behaviour.

The OBD connector exposes more than one CAN bus on some model years.  The
profile records bus/pin evidence and candidate services, but the device must
prove which bus and ECU answered before a manufacturer-specific decoder is
activated.  Standard SAE J1979 Mode 01 polling remains the safe baseline.
