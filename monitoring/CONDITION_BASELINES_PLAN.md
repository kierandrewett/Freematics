# Condition baselines

`collector/condition_baselines.py` compares a current trip with earlier trips
from the same device. It reports a measurement change. It does not define a
healthy value or diagnose a component.

## Reference selection

* A reference archive must be sealed and unchanged after sealing.
* It must have an earlier verified GNSS capture time than the target trip.
* It must have no indexed device-clock gap.
* Each context needs at least five reference trips. The contexts are cold idle,
  cold driving, warm idle, and warm driving. Driving uses matched RPM, load,
  and speed bins.
  Driving bins are compared separately and are never pooled.
* The result states `insufficient_reference_history` or
  `insufficient_current_coverage` when these conditions are not met.

## Measurement rules

* Each OBD value needs its paired `0x4xx` acquisition-age field. Device supply
  voltage uses `0x094`, and motion uses `0x095`. Missing age is unverified and
  is excluded.
* Each metric has its own age limit. This keeps valid older RPM evidence while
  it does not present a slower auxiliary PID as a one-second sample.
* A repeated acquisition timestamp is a held snapshot. The comparison counts
  it once.
* One-second coverage uses the gap between successive acquisition timestamps.
  A low value age by itself does not prove that the device acquired each value
  every second.
  The result separately reports observed acquisition cadence and full coverage.
  Full coverage also requires no stale, invalid, or age-unverified row and no
  frame gap above one second. A held snapshot is valid when its distinct source
  acquisitions remain within one second.
* A device tick reset starts a new epoch. No RPM spread or acquisition interval
  crosses that epoch.
* Idle uses speed at or below 1 km/h and RPM from 400 to 1500. Driving uses
  speed at or above 5 km/h and RPM at or above 400. Coolant 80 C separates
  cold from warm. These are analysis boundaries, not manufacturer limits.
* A metric needs at least three independent acquisitions in the current context
  and in each reference-trip summary. RPM spread needs three five-second
  windows.
* `telemetry_health` reports frame gaps above one second, tick resets, OBD
  timeout/failure counter increases, and missed-reading increases separately
  from vehicle comparisons.
* The result marks an observation as outside the reference trip-median band only
  when its median is outside the reference p10 to p90 band. It still does not
  diagnose a component.

## Failure cases and evidence

`collector/condition_baselines_test.py` indexes archive fixtures with the real
`HistoryIndexer`. It verifies these cases:

* future, unsealed, gapped, and time-unverified archives are not references;
* stale or unverified readings do not become fresh values;
* equal values at new timestamps remain distinct acquisitions;
* held timestamps are de-duplicated and do not prove one-second coverage;
* insufficient current samples and fewer than five reference trips are explicit;
* a device tick reset starts a separate acquisition epoch;
* the contextual result is included in `evidence_for_trip`.

Run the focused checks from `collector/`:

```sh
python3 -m unittest condition_baselines_test.py -v
python3 -m py_compile condition_baselines.py trip_intelligence.py mechanic_mcp.py
```
