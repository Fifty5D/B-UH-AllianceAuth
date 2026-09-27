# Private B-UH diagnostics candidate

The private [B-UH-Diagnostics repository](https://github.com/Fifty5D/B-UH-Diagnostics)
is the ChatGPT-facing read route. Its default `data` branch exposes one stable
`buh-diagnostics-latest.json` file and `evidence-index.json`. The GitHub
connector has fetched both **synthetic** files, a date/hour index, and a
redacted JSONL error shard from that private branch. A repo-scoped deploy key
has also refreshed the branch. Live retrieval remains
unverified until the separately approved host installation.

The collector identifies the four Auth services and their supporting services
using Docker Compose service labels (with an exact-name fallback for legacy
containers). Its approved service inventory includes expected replica counts;
missing beat or worker replicas make the report degraded even when other logs
are fresh. The inventory is part of the installation manifest and must be
rechecked during the fresh production preflight.

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
history. A SQLite insertion watermark republishes any retained hour receiving
late recovered logs, even after a collection outage longer than one hour. The
publisher prunes unreachable local Git objects whenever an hour expires and at
least daily otherwise; the local object cache cannot grow without bound. GitHub
may retain unreachable objects temporarily; the host's SQLite store is the
authoritative retention boundary.

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

After the immutable release and fresh read-only host preflight exist, create a
root-private canonical manifest using `ops/diagnostics/buh_diagnostics_install.py
manifest`. It binds the exact merged source commit, a `git archive` of that
commit, the installer/collector/publisher/redactor hashes, the repository-scoped
deploy-key fingerprint and hash, the pinned GitHub known-hosts hash, and the
current expected Compose project/services/replicas. Verify the archive digest
**before executing its installer**, then run the installer's read-only `verify`
mode. Present the complete manifest SHA-256 and release identity in the separate
production approval request. Keep the private key and manifest out of Git, logs,
PRs, and public artifacts.

After approval, run `install` with the exact confirmation `INSTALL BUH
DIAGNOSTICS <manifest SHA-256>`. It repeats payload verification, stages an
immutable root-only version, writes a durable transaction marker, stops the
diagnostics timers, atomically switches one `current` symlink, runs the first
collection and private publication, then enables both five-minute timers. It
records the approved identities in a root-only receipt. On ordinary failure it
restores the previous version and timer state; after process or host interruption,
run its `recover` command to reconcile the durable marker before retrying. It
does not run through ordinary application release automation.

The installation must verify the systemd services, host SQLite state, source
freshness, release identity, and live report/evidence fetched through the actual
ChatGPT GitHub connector. If publication fails, the previous private report
must be treated as stale; if collection fails, its source freshness/gaps must
prevent a healthy conclusion. Initial `earliest_retained_log_at` and per-source
first timestamps, rather than a configured 30-day target, state actual coverage.
