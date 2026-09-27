# Private B-UH diagnostics candidate

The private [B-UH-Diagnostics repository](https://github.com/Fifty5D/B-UH-Diagnostics)
is the ChatGPT-facing read route. Its default `data` branch exposes one stable
`buh-diagnostics-latest.json` file and `evidence-index.json`. The GitHub
connector has fetched both **synthetic** files, a date/hour index, and a
redacted JSONL error shard from that private branch. A repo-scoped deploy key
has also refreshed the branch. Live retrieval remains
unverified until the separately approved host installation.

## Host behavior after installation

`ops/diagnostics/buh_host_diagnostics.py` runs outside Auth and Celery every five
minutes. It stores redacted Docker container logs, Docker lifecycle events,
host warnings and Docker service journal entries, selected read-only application
task metadata, and resource samples in root-only SQLite. The store survives
container replacement and prunes records older than 30 days. It has a 25 GiB
free-space guard and bounds per-record and per-source reads. Missing containers,
failed commands, stale sources, truncation, and initial historical gaps appear
in the report. No permanent DEBUG logging is enabled.

`ops/diagnostics/buh_diagnostics_publish.py` uses a separate five-minute host
timer. It reads the already-redacted store, publishes a compact report, small
UTC-hour JSONL shards (at most 384 KiB each), hour/day indexes, and a top-level
index to the private `data` branch. It uses a write key valid only for that
repository and an officially pinned GitHub host key. The branch is replaced by
a fresh root commit each time, so expired records are removed from reachable
history. GitHub may retain unreachable objects temporarily; the host's SQLite
store is the authoritative retention boundary.

The present host had about 90 MB of current Docker JSON logs and about 3.66 MB
of journal entries in a 24-hour sample, with roughly 215 GiB free. This suggests
month-scale compressed host storage is manageable but is not a measured
30-day steady-state total. Check actual database and publisher directory size
after installation and weekly thereafter. Some older container logs have
already rotated and cannot be reconstructed.

## Access from ChatGPT/Work

Ask: “Read `buh-diagnostics-latest.json` from private repository
`Fifty5D/B-UH-Diagnostics` with the GitHub connector. Check `generated_at`,
`published_at`, `report_stale`, source freshness and coverage gaps. Then use
`evidence-index.json` to retrieve the relevant date/hour JSONL shard and
investigate [specific symptom].” The default branch is `data`, so no branch
argument is needed. The report is under 256 KiB; each evidence shard is under
384 KiB. An hour index gives counts by service and errors so the assistant can
choose evidence efficiently. The private repo remains readable if Auth's web
process is down, though it becomes stale if the host or GitHub transport stops.

## Separate production installation gate

Review the exact source commit and approved host payload before running
`ops/diagnostics/bootstrap-diagnostics.sh` as root with the collector,
publisher, redactor, repo-scoped private key, and pinned GitHub known-hosts file.
The script installs two root-owned systemd timers and starts the first
collection/publication. It does not run through ordinary application release
automation. Keep the private key out of Git, logs, PRs, and diagnostics.

The installation must verify the systemd services, host SQLite state, source
freshness, release identity, and live report/evidence fetched through the actual
ChatGPT GitHub connector. If publication fails, the previous private report
must be treated as stale; if collection fails, its source freshness/gaps must
prevent a healthy conclusion. Initial `earliest_retained_log_at` and per-source
first timestamps, rather than a configured 30-day target, state actual coverage.
