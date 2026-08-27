# GM GMLAN candidate layer

GMLAN services are a useful research namespace for Opel/Vauxhall ECUs.  Public
decoders describe ReadDataByIdentifier-style candidates such as VIN and an
odometer identifier, but those definitions are not proof that every Corsa-D
ECU exposes them at the same address.  Any candidate must be enabled only
after VIN/ECU identity and a positive response are captured.

Candidate observations are represented as evidence, not as active commands:

- service `0x1A` / identifier `0xDF`: ECU odometer candidate;
- service `0x22`: ECU data-identifier candidate family;
- service `0x1A` / identifier `0x90`: VIN candidate.

All are read-only, identity-gated, and experimental.  A negative response is
valuable capability evidence and must not trigger retries at high frequency.
