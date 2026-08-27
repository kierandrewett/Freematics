# GM GMLAN candidate layer

GMLAN services are a useful research namespace for Opel/Vauxhall ECUs.  Public
decoders describe ReadDataByIdentifier-style candidates such as VIN and an
odometer identifier, but those definitions are not proof that every Corsa-D
ECU exposes them at the same address.  Any candidate must be enabled only
after VIN/ECU identity and a positive response are captured.

Candidate observations are represented as evidence, not as active commands:

- service `0x1A` / identifiers `0x90`, `0x92`, `0x97`, `0x9A`, `0xB0`, `0xDE`, and `0xDF`: VIN, supplier, engine identity, diagnostic data, ECU address, module identity, and ECU odometer candidates;
- service `0x22`: ECU data-identifier candidate family;

All are read-only, identity-gated, and experimental.  A negative response is
valuable capability evidence and must not trigger retries at high frequency.
