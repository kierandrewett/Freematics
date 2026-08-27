# Opel / Vauxhall

The public Corsa-D evidence describes multiple CAN buses at the diagnostic
connector.  Exact ECU addresses, gateway routing, and data identifiers must
be discovered on the connected car rather than assumed from a forum or a
different engine variant.

The profile below therefore starts as `identity-gated`: it documents candidate
read-only observations and gives the collector a stable namespace, but it does
not enable proprietary polling by itself.
