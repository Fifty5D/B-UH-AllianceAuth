# ESI History Archive

Owned source for the existing `aa-buh-max-history` application. The 2.0.0
application source and original migrations were recovered from the installed
package; no production records or payloads are included. Distribution, Django
label, URLs, permissions and task names retain their existing identities. The
release planner builds the next patch version from this source.

## Collection and limits

There are two independent collectors:

| Collector | What it stores | When it runs |
| --- | --- | --- |
| ESI capture | Gzipped, distinct successful GET/HEAD response bodies, keyed by operation, parameters, page and character; repeated bodies update observation counts | When an installed app calls the supported synchronous or asynchronous django-esi client, including each pagination result |
| EVE Ref mirror | Catalog metadata and completed public downloads from the configured datasets | The existing `buh_max_history.tasks.scheduled_public_archive_sync` beat schedule; no second timer is created |

Capture is passive. Installing this app does not request every ESI endpoint or
grant new OAuth scopes. Member Audit and other callers control refresh and retry
schedules. Data already outside an ESI retention window cannot be reconstructed
unless another archive retained it. Distinct response bodies and first/last seen
counts are not a complete timestamped sequence of every repeated observation.
The public mirror holds the current downloaded version at each catalog path;
it does not yet retain separate revisions of a public file overwritten upstream.

The database configuration retains the operator's existing limits. Defaults are
12 download **attempts** and 4 GiB per run, a 25 GiB free-space reserve, and a
512 MiB capture response limit. Failures and partial transfers count toward
budgets. Large files that do not fit the configured per-run budget stay pending
and appear as `oversized_files`; they are not silently counted as saved.

Public batches reserve turns for new files, older backlog and due retries,
rotating across datasets. Failures back off from 15 minutes to at most 24 hours;
HTTP rate-limit responses pause the mirror for at least `Retry-After`. The
existing hourly timer provides the next attempt. Catalog work has a five-minute
budget and persistent per-index cursors. Download work stops at the total
20-minute checkpoint; Celery enforces a 30-minute soft and 31-minute hard limit.
Byte reads have socket timeouts and incremental deadline checks.

A MariaDB session advisory lock serializes scheduled, UI and management-command
jobs across containers. A dead worker loses that lock automatically. Only the
next lock holder marks abandoned RUNNING jobs failed and requeues interrupted
downloads. A live operation is never replaced merely because its timestamp is
old. SQLite development uses a process-owned filesystem lock. Losing the
MariaDB session stops the current batch before it can publish a downloaded file.

Catalog ETags and timestamps are compared in canonical form. HTTP header
formatting cannot reset a stored file to pending. Changed validators still
trigger refresh. Downloads use an adjacent temporary file and atomic replacement;
a failed transfer preserves the previous payload. Missing captured ESI files are
repaired when their successful response is observed again.

## Ordinary update

Use the existing source PR, validation and immutable release pipeline. This app
is registered alongside the other owned apps; it does not need a receiver patch,
a separate installer or a new beat schedule. The additive migration preserves
old rows and supplies database defaults for compatibility with the prior schema.
Run migrations and restart all application workers together with the approved
release so no old cache-lock collector remains active beside the new collector.
Keep the existing archive volume mounted for web and all workers.

After the approved installation, run `manage.py buh_archive_report` to check
stored/pending/failed counts and actual configuration. The next scheduled sync
recovers abandoned state automatically. `manage.py buh_archive_public_sync`
uses the same lock if an operator explicitly requests an immediate batch.

The platform's active recovery hold still applies. A source merge does not
install this app, publish a release, finish recovery or authorize deployment.
