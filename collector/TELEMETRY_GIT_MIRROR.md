# Raw telemetry Git mirror

`telemetry_git_mirror.py` is an optional, server-side mirror for people who
want to inspect the raw Freematics stream in GitHub. It is not the transport
protocol and it is not the source of truth:

1. `teleserver` appends every accepted HTTP payload to the raw archive.
2. The history indexer projects that archive into SQLite for Grafana trips.
3. This sidecar tails complete archive lines into a durable local spool.
4. Every flush writes immutable JSONL segments and creates one robot-authored
   Git commit. The local commit is the durable acknowledgement; push failures
   leave it ahead of the remote and the next cycle retries that commit.

Each upload has a `batch` record with exact `raw_payload_b64` bytes and each
PID-0-delimited frame has a `sample` record with exact `raw_frame_b64` bytes,
an ordered field list, a deterministic `event_id`, and checksum status. This
keeps unknown and duplicate fields inspectable. `mirror_observed_at` is only
when the sidecar read the archive; it must not be used as collector receipt
time or capture time.

Recommended layout:

```text
events/<device>/<year>/<month>/<day>/<trip>/part-<event-id>.jsonl
```

The GitHub repository must be private. Never put SIM, Caddy, GitHub, or
telemetry endpoint credentials in the repository. The server still needs a
separate credential (prefer a fine-grained token restricted to this one repo)
to push. GitHub is a browseable secondary copy; Grafana and the mechanic/MCP
tools read the local raw archive/SQLite projection instead.

Example one-shot smoke test:

```bash
python3 collector/telemetry_git_mirror.py \
  --archive-root /config/apps/freematics/data \
  --repo /config/apps/freematics/telemetry-git \
  --state-dir /config/apps/freematics/telemetry-git-state \
  --once --no-push
```

For production, run it as a single restartable sidecar with a writable state
volume and `--flush-seconds 120`. Use one writer only; do not run multiple
replicas against the same spool or Git checkout.
