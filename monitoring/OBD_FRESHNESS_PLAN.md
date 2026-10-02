# Continuous OBD acquisition

Goal: acquire every supported live Mode 01 reading within 1,000 ms, with
250 ms targets for RPM and speed. A repeated row is not a new measurement.
This is the acquisition foundation for general vehicle condition monitoring.

## Evidence and choices

- The previous scheduler allocates only four auxiliary requests per second.
  The recorded Corsa data has approximately seven-second auxiliary gaps.
- `kierandrewett/obd` queries its selected PIDs consecutively with zero default
  delays. Its Full selection contains 20 PIDs, versus 33 observed on this car.
- Python-OBD and ELMduino support an expected-response-count suffix. Official
  Freematics source does not establish support for it, ATAT or ATST. Keep the
  existing bridge commands, timeout and baud rate until bench tests establish
  compatibility. The recorded 57 ms cycle maximum is not mean request latency.
- Use consecutive, sequential requests selected by the largest fraction of
  the freshness interval already used. This removes the quota without parallel requests or an unbounded catalogue loop.
- Keep the existing value/age wire contract. Correct the collector's age metric
  so held values do not appear newly acquired. No new telemetry fields.

## Failure cases recorded before scheduler implementation

- Quotas leave auxiliary readings several seconds old even with a fast ECU.
- Short deadlines can starve other PIDs; catalogue order can bias equal deadlines.
- A failed PID can monopolise retries or falsely update its value or timestamp.
- A slow or absent ECU can block other sensors if a whole scan holds the mutex.
- Unsupported PIDs must not generate requests or invented zero values.
- Timestamp rollover, initial acquisition and reconnect can corrupt deadlines.
- Success on an auxiliary PID must not hide repeated RPM or speed failures.
- Trouble-code scans must remain available; they have a separate, slower cadence
  and their existing bridge exchange can exceed the live freshness target.
  Upstream retries requests but does not document resumable response pages.
  Interleaving live commands inside that exchange needs physical bridge proof.
- A repeated cached value can appear fresh if the collector ignores its age.
- A split serial completion prompt can cause an unnecessary full timeout.

## Delivery and evidence

- [x] Reproduce and fix split serial prompt handling with the actual receive code.
- [x] Run the actual scheduler with a scripted ECU before and after the change.
- [x] Replace the auxiliary quota and retain bounded failure recovery.
- [x] Verify acquisition age through the real local collector HTTP endpoint.
- [x] Run firmware build, sampler/lifecycle checks and the collector emulator.
- [x] Record the changed cadence and physical acceptance procedure in the car runbook.
- [ ] Verify the bridge fault-code exchange before changing its scheduling.
- [ ] Deploy the collector change, flash and measure the installed bridge/car across all supported PIDs.

Host simulation cannot prove ECU throughput, task contention or in-car behaviour.
The physical acceptance criterion is the maximum gap between distinct successful
acquisitions of each supported live PID, measured across warm idle and driving.
Report missed targets and diagnostic-scan intervals, not only averages. Upload
batching and connectivity also determine when measurements reach Grafana.

## Prior art

- [User app polling](https://github.com/kierandrewett/obd/blob/b56a07a2ffc0d574e68e6426ff55418ee4173b7a/src/obd_ops.rs#L121-L157)
- [Python-OBD response count](https://github.com/brendan-w/python-OBD/blob/a378bdd81d58c67d08050e4244173a9a7dbda73d/obd/obd.py#L300-L314)
- [ELMduino response count](https://github.com/PowerBroker2/ELMduino/blob/e9d6b55fd63f4afd7789d0981861e9990cdfcd8e/src/ELMduino.cpp#L610-L636)

## Host results

The scheduler harness compiles the real polling functions. At an explicit
10 ms reply cost plus a 10 ms worker interval, the old maximum successive
reading gap was 6,750 ms. The new maximum is 860 ms, with 240 ms for RPM and
speed. The first complete sweep takes 1,010 ms. Initial acquisition and real
fault-code scan pauses are not included in the successive-gap result.
The latter are stubbed in this scheduling harness and remain a known gap in
the strict one-second requirement. Slower-link cases verify fair progress,
not compliance with a rate the simulated link cannot supply.

Repeat from the repository root:

```sh
python3 tools/check-obd-scheduling.py --report /tmp/obd-scheduling.json
python3 tools/check-obd-scheduling.py --source-ref 274dd0c --report /tmp/obd-baseline.json
python3 tools/check-obd-uart.py --report /tmp/obd-uart.json
python3 tools/check-collector-sampling.py
python3 tools/emulator/run.py --strict --collector --report /tmp/obd-emulator.json
pio run -e esp32dev
```

The baseline command intentionally returns a failure for the old freshness
policy. The UART harness reproduces the prior split-prompt timeout. The local
collector test reproduces and fixes a held 10-second value reported as fresh.
These are host results, not installed-device or live-server verification.

The attempted resumable DTC refactor was removed during review. Upstream
`readDTC` stops retrying after a non-`NO DATA` response; it does not document
response pages that survive intervening Mode 01 queries. Keep the current
exchange until a bridge capture establishes that contract. See
[upstream routine](https://github.com/stanleyhuangyc/Freematics/blob/master/libraries/FreematicsPlus/FreematicsOBD.cpp#L119-L152).

Final verification also passed the sampler boundary, recorder custody, interval
extremes and power lifecycle checks. `similarity-py tools` found two existing
helper/scenario similarities; neither required changes for this work. No USB
serial device was present. The collector changes have not been deployed and
the firmware has not been flashed.
